from flask import Blueprint, request
from functools import lru_cache
from agent.utils.ts_client import TimeSeriesClient
from twa import agentlogging
import pandas as pd
import jwt
from jwt import PyJWKClient
from pandas._libs.tslibs.np_datetime import OutOfBoundsDatetime
from agent.trip.utilities import wgs_to_utm_code
from agent.trip.trip_detection import detect_trips, CH_TRIP_INDEX
from py4j.java_gateway import JavaObject
from agent.trip.kg_client import KgClient
from agent.utils.stack_gateway import stack_clients_view
from agent.utils.postgis_client import postgis_client
from agent.utils.env_configs import KEYCLOAK_REALM, KEYCLOAK_SERVER

logger = agentlogging.get_logger('dev')

ROUTE = "/process_trajectory"
TIMELINE_ROUTE = "/process_trajectory_for_timeline"

process_trajectory_bp = Blueprint('process_trajectory_bp', __name__)


@process_trajectory_bp.route(ROUTE, methods=['POST'])
def api():
    logger.info('Received request to process trajectory')
    iri = request.args['iri']
    upperbound = request.args.get('upperbound')
    lowerbound = request.args.get('lowerbound')

    return process_trajectories([iri], lowerbound, upperbound, iri)


@process_trajectory_bp.route(TIMELINE_ROUTE, methods=['POST'])
def process_trajectory_for_timeline():
    """Process all trajectories owned by the authenticated Keycloak user."""
    try:
        user_id = get_authenticated_user_id(request.headers.get('Authorization'))
    except AuthenticationError as ex:
        logger.warning(str(ex))
        return str(ex), 401, {'WWW-Authenticate': 'Bearer'}

    kg_client = KgClient()
    point_iris = kg_client.get_point_iris(user_id)
    if not point_iris:
        return 'No trajectory points found for authenticated user', 404

    return process_trajectories(
        point_iris,
        request.args.get('lowerbound'),
        request.args.get('upperbound'),
        user_id,
        kg_client=kg_client,
    )


class AuthenticationError(Exception):
    pass


@lru_cache(maxsize=1)
def get_keycloak_jwks_client():
    issuer = f"{KEYCLOAK_SERVER.rstrip('/')}/realms/{KEYCLOAK_REALM}"
    return PyJWKClient(f"{issuer}/protocol/openid-connect/certs")


def get_authenticated_user_id(authorization_header):
    if not authorization_header or not authorization_header.lower().startswith('bearer '):
        raise AuthenticationError('Bearer token is missing')

    token = authorization_header[7:].strip()
    if not token:
        raise AuthenticationError('Bearer token is missing')
    if not KEYCLOAK_SERVER or not KEYCLOAK_REALM:
        raise AuthenticationError('Keycloak authentication is not configured')

    issuer = f"{KEYCLOAK_SERVER.rstrip('/')}/realms/{KEYCLOAK_REALM}"
    try:
        signing_key = get_keycloak_jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=['RS256'],
            issuer=issuer,
            options={'require': ['exp', 'sub'], 'verify_aud': False},
        )
    except jwt.PyJWTError as ex:
        raise AuthenticationError('Invalid or expired bearer token') from ex

    user_id = claims.get('sub')
    if not isinstance(user_id, str) or not user_id.strip():
        raise AuthenticationError('Bearer token subject is missing')
    return user_id


def process_trajectories(point_iris, lowerbound, upperbound, participant_id,
                         kg_client=None):
    """Detect one trajectory across several point time series and persist per point."""
    kg_client = kg_client or KgClient()
    trajectory_frames = []

    for point_iri in point_iris:
        point_upperbound = upperbound
        point_lowerbound = lowerbound
        if upperbound is not None:
            point_upperbound = kg_client.convert_input_time_for_timeseries(
                time=upperbound, point_iri=point_iri)
        if lowerbound is not None:
            point_lowerbound = kg_client.convert_input_time_for_timeseries(
                time=lowerbound, point_iri=point_iri)

        logger.info('Querying time series data for %s', point_iri)
        dataframe, _, time_list_for_java = kg_client.get_trajectory_time_series(
            point_iri=point_iri,
            lowerbound=point_lowerbound,
            upperbound=point_upperbound,
        )
        if dataframe.empty:
            continue

        dataframe = dataframe.copy()
        dataframe['_point_iri'] = point_iri
        dataframe['_time_for_java'] = time_list_for_java
        trajectory_frames.append(dataframe)

    if not trajectory_frames:
        message = 'Time series data is empty'
        logger.error(message)
        return message

    dataframe = pd.concat(trajectory_frames, ignore_index=True)
    dataframe = dataframe.sort_values('utc_date').reset_index(drop=True)
    utm_code = wgs_to_utm_code(dataframe.iloc[0]['lat'], dataframe.iloc[0]['lon'])

    columns = {
        "utc_date": "utc_date",
        "lat": "lat",
        "lon": "lon",
        "utm_n": "utm_n",
        "utm_e": "utm_e"
    }

    logger.info('Running trip detection code')

    try:
        detected_gps, _, _, _, _ = detect_trips(
            dataframe,
            participant_id,
            columns,
            interpolate_helper_func=None,
            code=utm_code
        )
    except Exception as ex:
        err_msg = 'Failed to run trip processing code: ' + str(ex)
        logger.error(err_msg)
        raise

    for point_iri, point_data in detected_gps.groupby('_point_iri', sort=False):
        time_series_client = TimeSeriesClient(point_iri)
        trip = kg_client.get_trip(point_iri)

        if trip is None:
            logger.info('Trip does not exist for %s, instantiating', point_iri)
            trip = kg_client.instantiate_trip()
            postgis_client.add_point_to_trip(point_iri=point_iri, trip_iri=trip)
            time_series_iri = kg_client.get_time_series_iri(point_iri)
            time_series_client.add_columns(
                time_series_iri=time_series_iri,
                data_iri=[trip],
                class_list=[stack_clients_view.java.lang.Integer.TYPE],
            )

        # py4j requires Python-native int values; pandas scalars will not work.
        trip_list_int = [int(x) for x in point_data[CH_TRIP_INDEX]]
        time_series_trip_visit = time_series_client.create_time_series(
            times=point_data['_time_for_java'].tolist(),
            data_iri_list=[trip],
            values=[trip_list_int],
        )
        time_series_client.add_time_series(time_series=time_series_trip_visit)

    return 'Added trip data'


def convert_time_series_to_dataframe(time_series, point_iri: str):
    original_time_list = time_series.getTimes()

    # convert timestamps from TWA time series into pandas timestamps
    if isinstance(original_time_list[0], JavaObject):
        timestamps = []
        # assume something like Instant
        for time in original_time_list:
            timestamps.append(pd.to_datetime(time.toString()))
    else:
        # probably epoch
        try:
            # OutOfBoundsDatetime exception might be thrown here
            pd.to_datetime(original_time_list[0], unit='s')
            timestamps = pd.to_datetime(original_time_list, unit='s')
        except OutOfBoundsDatetime:
            # assume milliseconds
            timestamps = pd.to_datetime(original_time_list, unit='ms')

    postgis_point_list = time_series.getValuesAsPoint(point_iri)

    lat, lon = zip(*[(p.getY(), p.getX()) for p in postgis_point_list])

    utm_code = wgs_to_utm_code(lat[0], lon[0])

    return pd.DataFrame({'utc_date': timestamps, 'lat': lat, 'lon': lon}), utm_code
