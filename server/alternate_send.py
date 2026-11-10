import base64
import binascii
import csv
import datetime
import json
import os

from flask import Flask, jsonify, request

app = Flask(__name__)
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scans.csv")
FIELDS = ["timestamp", "type", "raw", "lat", "lon", "lat_e7", "lon_e7", "key", "value"]


