from agent.utils.stack_gateway import stack_clients_view
from agent.utils.stack_configs import BLAZEGRAPH_URL, STACK_OUTGOING
import uuid
import json
from py4j.java_gateway import JavaObject
import pandas as pd
from shapely import wkt
from agent.trip.utilities import wgs_to_utm_code
import requests
from urllib.parse import urlsplit
from agent.utils.env_configs import NAMESPACE

PREFIX = 'https://www.theworldavatar.com/kg/ontoexposure/'
TRIP = PREFIX + 'Trip'
TIMESERIES_NAMESPACE = 'https://www.theworldavatar.com/kg/ontotimeseries/'
HAS_TIME_SERIES = TIMESERIES_NAMESPACE + 'hasTimeSeries'
HAS_TIME_CLASS = TIMESERIES_NAMESPACE + 'hasTimeClass'


class KgClient():
    def __init__(self):
        self.remote_store_client = stack_clients_view.RemoteStoreClient(
            STACK_OUTGOING, BLAZEGRAPH_URL)

        # check if namespace exists, if not initialise
        r = requests.head(BLAZEGRAPH_URL)

        if r.status_code != 200:
            # get the front part of the url
            parsed_url = urlsplit(BLAZEGRAPH_URL)
            url = f"{parsed_url.scheme}://{parsed_url.netloc}" + \
                '/blazegraph/namespace'

            props = (
                f"com.bigdata.rdf.sail.namespace={NAMESPACE}\n"
                f"com.bigdata.rdf.store.AbstractTripleStore.quads=false\n"
                f"com.bigdata.rdf.store.AbstractTripleStore.axiomsClass=com.bigdata.rdf.axioms.NoAxioms\n"
            )

            r = requests.post(url, data=props, headers={
                              "Content-Type": "text/plain"})

            if r.status_code not in (200, 201):
                raise RuntimeError(
                    f"Failed to create namespace '{NAMESPACE}': {r.status_code} {r.text}")

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
                condition = f""" ?timestamp >= "{lowerbound[0].toString()}"^^xsd:dateTime"""
            else:
                condition = f"""?time_number >= {lowerbound}"""
            conditions.append(condition)

        if upperbound is not None:
            if isinstance(upperbound, JavaObject):
                condition = f"""?timestamp <= "{upperbound[0].toString()}"^^xsd:dateTime"""
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
                timeseries:hasResult/timeseries:hasValue ?val.
            OPTIONAL {{?obs time:hasTime/time:inXSDDateTime ?timestamp.}}
            OPTIONAL {{?obs time:hasTime/time:inTimePosition/time:numericPosition ?time_number.}}
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

        if len(query_results_parsed) == 0:
            return pd.DataFrame()

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
            class_name = self.get_java_time_class(point_iri)
            return stack_clients_view.TimeSeriesClientFactory.timestampFactory(class_name, time)
