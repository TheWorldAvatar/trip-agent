from agent.utils.baselib_gateway import baselib_view, jpsBaseLibGW
from agent.utils.java_time_parser import JavaTimeParser
from agent.utils.stack_configs import BLAZEGRAPH_URL, STACK_OUTGOING
import uuid
import json
from py4j.java_gateway import JavaObject
import pandas as pd
from shapely import wkt
from agent.trip.utilities import wgs_to_utm_code

PREFIX = 'https://www.theworldavatar.com/kg/ontoexposure/'
TRIP = PREFIX + 'Trip'
TIMESERIES_NAMESPACE = 'https://www.theworldavatar.com/kg/ontotimeseries/'
HAS_TIME_SERIES = TIMESERIES_NAMESPACE + 'hasTimeSeries'
HAS_TIME_CLASS = TIMESERIES_NAMESPACE + 'hasTimeClass'


class KgClient():
    def __init__(self):
        self.remote_store_client = baselib_view.RemoteStoreClient(
            STACK_OUTGOING, BLAZEGRAPH_URL)

    def get_trip(self, point_iri: str):
        query = f"""
        SELECT ?trip
        WHERE {{
            <{point_iri}> <{HAS_TIME_SERIES}> ?time_series.
            ?trip <{HAS_TIME_SERIES}> ?time_series; a <{TRIP}>.
        }}
        """
        query_results = self.remote_store_client.executeQuery(query)

        trip = None

        if not query_results.isEmpty():
            trip = query_results.getJSONObject(0).getString('trip')

        return trip

    def get_time_series_iri(self, point_iri: str):
        query = f"""
        SELECT ?time_series
        WHERE {{
            <{point_iri}> <{HAS_TIME_SERIES}> ?time_series.
        }}
        """
        query_results = self.remote_store_client.executeQuery(query)
        return query_results.getJSONObject(0).getString('time_series')

    def instantiate_trip(self):
        trip = PREFIX + 'trip/' + str(uuid.uuid4())
        query = f"""
        INSERT DATA {{<{trip}> a <{TRIP}>}}
        """
        self.remote_store_client.executeUpdate(query)
        return trip

    def get_java_time_class(self, point_iri: str):
        query = f"""
        SELECT ?time_class
        WHERE {{
            <{point_iri}> <{HAS_TIME_SERIES}>/<{HAS_TIME_CLASS}> ?time_class.
        }}
        """
        query_results = self.remote_store_client.executeQuery(query)
        return query_results.getJSONObject(0).getString('time_class')

    def get_trajectory_time_series(self, point_iri: str, lowerbound, upperbound):
        conditions = []

        if lowerbound is not None:
            if isinstance(lowerbound, JavaObject):
                condition = f""" ?timestamp >= "{lowerbound.toString()}"^^xsd:dateTime"""
            else:
                condition = f"""?time_number >= {lowerbound}"""
            conditions.append(condition)

        if upperbound is not None:
            if isinstance(upperbound, JavaObject):
                condition = f"""?timestamp <= "{upperbound.toString()}"^^xsd:dateTime"""
            else:
                condition = f"""?time_number <= {upperbound}"""
            conditions.append(condition)

        filter_clause = ''
        if conditions:
            filter_clause = f"FILTER ({' && '.join(conditions)})"

        query = f"""
        PREFIX time: <http://www.w3.org/2006/time#>
        PREFIX timeseries: <https://www.theworldavatar.com/kg/ontotimeseries/>
        PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
        SELECT ?timestamp ?time_number ?val
        WHERE {{
            ?obs timeseries:observationOf <{point_iri}>;
                time:hasTime/time:inXSDDateTime ?timestamp;
                time:hasTime/time:inTimePosition/time:numericPosition ?time_number;
                timeseries:hasResult/timeseries:hasValue ?val.
            {filter_clause}
        }}
        ORDER BY ?timestamp ?time_number
        """

        query_results = self.remote_store_client.executeQuery(query)
        query_results_parsed = json.loads(query_results.toString())

        timestamp_list_as_string = []
        timenumber_list_as_string = []
        lat = []
        lon = []

        for row in query_results_parsed:
            if 'timestamp' in row:
                timestamp_list_as_string.append(row['timestamp'])
            if 'time_number' in row:
                timenumber_list_as_string.append(row['time_number'])

            point = wkt.loads(row['val'])
            lon.append(point.x)
            lat.append(point.y)

        if timestamp_list_as_string:
            timestamps = pd.to_datetime(
                timestamp_list_as_string, format='ISO8601')
        else:
            time_number_list = [float(t) for t in timenumber_list_as_string]
            timestamps = pd.to_datetime(time_number_list, unit='s')

        utm_code = wgs_to_utm_code(lat[0], lon[0])

        # if it is not a timestamp, save to assume that it is a number
        if timestamp_list_as_string:
            time_list_for_java = self.convert_input_time_for_timeseries(
                time=timestamp_list_as_string, point_iri=point_iri)
        else:
            time_list_for_java = self.convert_input_time_for_timeseries(
                time=timenumber_list_as_string, point_iri=point_iri)

        return pd.DataFrame({'utc_date': timestamps, 'lat': lat, 'lon': lon}), utm_code, time_list_for_java

    def convert_input_time_for_timeseries(self, time, point_iri: str):
        # assumes time is in seconds or milliseconds, if an exception is thrown,
        # queries the time class from KG (e.g. java.time.Instant) and use the
        # parse method to parse time into the correct Java object
        try:
            # assume epoch seconds
            if isinstance(time, list):
                return [float(t) for t in time]
            else:
                return float(time)
        except (ValueError, TypeError):
            # lots of trial and error done to get Java reflection to work correctly!
            class_name = self.get_java_time_class(point_iri)
            time_parser = JavaTimeParser()
            if isinstance(time, list):
                time_list = []
                for t in time:
                    time_list.append(time_parser.parse_java_time(
                        class_name=class_name, time_str=t))
                    # time_list.append(self._parse_java_time(class_name, t))
                return time_list

            else:
                return time_parser.parse_java_time(class_name=class_name, time_str=time)
