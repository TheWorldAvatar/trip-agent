from flask import Blueprint, request
from agent.utils.ts_client import TimeSeriesClient
from twa import agentlogging
import pandas as pd
from pandas._libs.tslibs.np_datetime import OutOfBoundsDatetime
from agent.trip.utilities import wgs_to_utm_code
from agent.trip.trip_detection import detect_trips, CH_TRIP_INDEX
from py4j.java_gateway import JavaObject
from agent.trip.kg_client import KgClient
from agent.utils.stack_gateway import stack_clients_view
from agent.utils.postgis_client import postgis_client

logger = agentlogging.get_logger('dev')

ROUTE = "/process_trajectory"

process_trajectory_bp = Blueprint('process_trajectory_bp', __name__)


@process_trajectory_bp.route(ROUTE, methods=['POST'])
def api():
    logger.info('Received request to process trajectory')
    iri = request.args['iri']
    upperbound = request.args.get('upperbound')
    lowerbound = request.args.get('lowerbound')

    time_series_client = TimeSeriesClient(iri)
    kg_client = KgClient()

    # convert upperbound and lowerbound into the correct types from string
    if upperbound is not None:
        upperbound = kg_client.convert_input_time_for_timeseries(
            time=upperbound, point_iri=iri)

    if lowerbound is not None:
        lowerbound = kg_client.convert_input_time_for_timeseries(
            time=lowerbound, point_iri=iri)

    logger.info('Querying time series data')

    dataframe, utm_code, time_list_for_java = kg_client.get_trajectory_time_series(
        point_iri=iri, lowerbound=lowerbound, upperbound=upperbound)

    if len(dataframe) == 0:
        message = 'Time series data is empty'
        logger.error(message)
        return message

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
            iri,
            columns,
            interpolate_helper_func=None,
            code=utm_code
        )
    except Exception as ex:
        err_msg = 'Failed to run trip processing code: ' + str(ex)
        logger.error(err_msg)
        raise

    trip = kg_client.get_trip(iri)

    if trip is None:
        logger.info('Trip does not exist, instantiating')
        trip = kg_client.instantiate_trip()
        postgis_client.add_point_to_trip(point_iri=iri, trip_iri=trip)
        time_series_iri = kg_client.get_time_series_iri(iri)
        time_series_client.add_columns(time_series_iri=time_series_iri, data_iri=[
            trip], class_list=[stack_clients_view.java.lang.Integer.TYPE])

    # py4j requires python native int, pandas array won't work
    trip_list_int = [int(x) for x in detected_gps[CH_TRIP_INDEX]]

    # create time series object for upload to time series database
    time_series_trip_visit = time_series_client.create_time_series(
        times=time_list_for_java, data_iri_list=[trip], values=[trip_list_int])

    # upload to database
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
