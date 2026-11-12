from flask import Blueprint, request
from functools import lru_cache
from agent.jobs.store import jobs
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

    return queue_job({'point_iris': [iri], 'lowerbound': lowerbound, 'upperbound': upperbound})


@process_trajectory_bp.route(TIMELINE_ROUTE, methods=['POST'])
def process_trajectory_for_timeline():
    """Process all trajectories owned by the authenticated Keycloak user."""
    try:
        user_id = get_authenticated_user_id(request.headers.get('Authorization'))
    except AuthenticationError as ex:
        logger.warning(str(ex))
        return str(ex), 401, {'WWW-Authenticate': 'Bearer'}

    return queue_job({
        'lowerbound': request.args.get('lowerbound'),
        'upperbound': request.args.get('upperbound'),
    }, owner_id=user_id)


def queue_job(payload, owner_id=None):
    job_id = jobs.enqueue(payload, owner_id=owner_id)
    logger.info('Queued trip job %s', job_id)
    response = {'job_id': job_id, 'status': 'queued'}
    if owner_id is not None:
        response['status_url'] = 'jobs/active'
    return response, 202


@process_trajectory_bp.route('/jobs/active', methods=['GET'])
def active_jobs():
    try:
        user_id = get_authenticated_user_id(request.headers.get('Authorization'))
    except AuthenticationError as ex:
        return {'error': str(ex)}, 401, {'WWW-Authenticate': 'Bearer'}
    public_jobs = []
    for job in jobs.active_for_owner(user_id):
        public = {key: value for key, value in job.items() if key != 'owner_id'}
        for key in ('created_at', 'started_at', 'finished_at', 'heartbeat_at'):
            if public.get(key) is not None:
                public[key] = public[key].isoformat()
        public_jobs.append(public)
    return public_jobs


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
                         kg_client=None, update_timeline_table=False, progress=None):
    """Detect one trajectory across several point time series and persist per point."""
    kg_client = kg_client or KgClient()
    progress = progress or (lambda stage: None)
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

        progress('reading_trajectory')
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

    progress('detecting_trips')
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

    progress('uploading_trip_time_series')
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

    logger.info('Finished uploading trip time series for %d trajectories (%d points)',
                len(point_iris), len(detected_gps))
    if update_timeline_table:
        progress('publishing_timeline_trips')
        logger.info('Preparing shared-table trip assignments for %d points', len(detected_gps))
        # One transaction publishes the completed result across all trajectories.
        assignments = []
        for point_iri, point_data in detected_gps.groupby('_point_iri', sort=False):
            for original_time, trip_index in zip(
                    point_data['_time_for_java'], point_data[CH_TRIP_INDEX]):
                if isinstance(original_time, JavaObject):
                    # Match the uploader's time_as_number representation.
                    source_time = int(original_time.toEpochMilli()) / 1000.0
                else:
                    source_time = float(original_time)
                assignments.append((point_iri, source_time, int(trip_index)))
        logger.info('Prepared %d trip assignments; publishing to trajectory_points', len(assignments))
        postgis_client.update_timeline_trips(assignments)
        logger.info('Shared-table trip publication completed: %d points', len(assignments))

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
