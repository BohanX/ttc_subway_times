from uuid import uuid4
import json
import datetime
import logging
import os

import boto3
from botocore.exceptions import ClientError
import pytz
from retrying import retry


LOGGER = logging.getLogger(__name__)

if os.environ.get("LOG_LEVEL"):
    LOGGER.setLevel(getattr(logging, os.environ.get("LOG_LEVEL")))
else:
    LOGGER.setLevel(getattr(logging, "INFO"))


class WriteSQL(object):
    def __init__(self, schema, conn):

        self.schema = schema
        self.conn = conn
        self.cursor = None

        self.requests_sql = """INSERT INTO {schema}.requests(data_,
                        stationid, lineid, all_stations, create_date, pollid, request_date)
                       VALUES(%(data_)s, %(stationid)s, %(lineid)s, %(all_stations)s, %(create_date)s, %(pollid)s, %(request_date)s)
                       RETURNING requestid""".format(
            schema=schema
        )
        self.poll_update_sql = """UPDATE {schema}.polls set poll_end = %s
                        WHERE pollid = %s""".format(
            schema=schema
        )
        self.poll_insert_sql = """INSERT INTO {schema}.polls(poll_start)
                        VALUES(%s)
                        RETURNING pollid""".format(
            schema=schema
        )

        self.ntas_sql = """INSERT INTO {schema}.ntas_data(\
            requestid, id, station_char, subwayline, system_message_type, \
            timint, traindirection, trainid, train_message, train_dest) \
            VALUES (%(requestid)s, %(id)s, %(station_char)s, %(subwayline)s, %(system_message_type)s, \
            %(timint)s, %(traindirection)s, %(trainid)s, %(train_message)s, %(train_dest)s);
          """.format(
            schema=self.schema
        )

        self.cursor = self.conn.cursor()

    def add_ntas_record(self, record_row):
        self.cursor.execute(self.ntas_sql, record_row)

    def add_request_info(self, request_row):
        self.cursor.execute(self.requests_sql, request_row)
        request_id = self.cursor.fetchone()[0]

        return request_id

    def add_poll_start(self, time):
        self.cursor.execute(self.poll_insert_sql, (str(time),))
        poll_id = self.cursor.fetchone()[0]

        return poll_id

    def add_poll_end(self, poll_id, time):
        self.cursor.execute(self.poll_update_sql, (str(time), str(poll_id)))

    def commit(self):
        self.conn.commit()
        self.cursor.close()


class WriteS3(object):
    def __init__(self, bucket_name, aws_access_key=None, aws_secret_access_key=None):
        self.request_poll_id={}

        self.output_jsons = {}

        self.bucket_name = bucket_name

        if aws_access_key==aws_secret_access_key and (aws_access_key is None):
            self.s3 = boto3.client('s3')
        else:
            self.s3 = boto3.client('s3', aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_access_key)

    def add_ntas_record(self, record_row):
        request_id = record_row['requestid']
        poll_id = self.request_poll_id[request_id]

        x = {i: record_row[i] for i in record_row if i != 'requestid'}
        self.output_jsons[poll_id]['requests'][request_id]['responses'].append(x)

    def add_poll_start(self, time):

        poll_id = str(uuid4())

        self.output_jsons[poll_id] = {"pollid": poll_id, "start": str(time), "requests":{}}

        return poll_id

    def add_poll_end(self, poll_id, time):
        self.output_jsons[poll_id]["end"]=str(time)

    def add_request_info(self, request_row):
        request_id = str(uuid4())
        poll_id = request_row['pollid']

        self.request_poll_id[request_id] = poll_id

        self.output_jsons[poll_id]['requests'][request_id] = {i: request_row[i] for i in request_row if i != 'pollid'}
        self.output_jsons[poll_id]['requests'][request_id]['responses'] = []

        return request_id

    @staticmethod
    def _service_day(datetimestamp, servicedayhour=4):
        """Will round down a timestamp to the previous service day

        Times before servicedayhour will get rounded down to the previous day

        :param timestamp: A datetime to get the service day of
        :param servicedayhour: The cut time to go to the previous sercice day
        :return: A datetime.date of the service day
        """
        if datetimestamp.time() < datetime.time(servicedayhour, 0, 0):
            return datetimestamp.date() - datetime.timedelta(days=1)

        return datetimestamp.date()

    @retry(stop_max_attempt_number=5)
    def commit(self, timestamp = None, timezone="America/Toronto"):
        """Will upload our collected polls and responses to S3

        :return:
        """

        LOGGER.info("Writing records to S3")

        if timestamp is None:
            tz = pytz.timezone(timezone)
            timestamp = datetime.datetime.now(tz)

        timestamp_str = str(timestamp).replace(':', '_').replace(' ', '.')
        service_date = self._service_day(timestamp)

        o = self.output_jsons.copy()

        for pollid, poll in o.items():
            poll.pop('pollid', None)
            poll['requests']=[v for _, v in poll['requests'].items()]

        try:
            s3_path = '{service_date}/{timestamp_str}.json'.format(service_date=service_date,
                                                                   timestamp_str=timestamp_str)

            self.s3.put_object(
                Bucket=self.bucket_name,
                Body=json.dumps([v for _, v in o.items()]),
                Key=s3_path
            )
        except ClientError as e:
            LOGGER.critical("Error writing to S3, retrying: {e}".format(e=e))
