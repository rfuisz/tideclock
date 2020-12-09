

import urllib.parse
import urllib.request
import json
import time
from datetime import datetime

from pprint import pprint

default_tide_height = 5.9
default_station = '9414290'

def NOAA_checker(tide_cutoff=default_tide_height, station = default_station):
	api = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?"

	post_params =  {'date'     : 'today',
					'station'  : str(station),
					'product'  : 'predictions',
					'datum'    : 'MLLW',
					'time_zone': 'gmt',
					'units'    : 'english',
					'format'   : 'json'}
	post_args = urllib.parse.urlencode(post_params)
	api_url = api+post_args
	print(api_url)
	request = urllib.request.urlopen(api_url)
	data = json.load(request)
	tide_cutoff = int(tide_cutoff)
	flood_tides         = []
	ebb_tides           = []
	flood_and_ebb_tides = [data['predictions'][0]]
	first_tide = float(data['predictions'][0]['v'])
	high_tide = False
	if first_tide > tide_cutoff:
		high_tide = True

	print("NOAA Suggests these tides:")
	for time_point in data['predictions']:
		tide = float(time_point['v'])
		time = str(time_point['t'])
		above_minimum_height = False
		if float(tide) > tide_cutoff:
			above_minimum_height = True
		if above_minimum_height and not high_tide:
			flood_and_ebb_tides.append(time_point)
			print("flood tide "+str(tide) + " at time " + time)
			high_tide = True
		if not above_minimum_height and high_tide:
			flood_and_ebb_tides.append(time_point)
			print("ebb tide "+ str(tide) + " at time " + time)
			high_tide = False
	return flood_and_ebb_tides
def store_tides(tides, tides_filename ="NOAA_tides.json" ):
	f = open(tides_filename, "w")
	f.write(json.dumps(tides, indent = 2))
	f.close()
def get_stored_tides(tides_filename = "NOAA_tides.json"):
	f = open(tides_filename)
	tides_json = json.load(f)
	f.close()
	return tides_json
def update_stored_tides(tide_height    = default_tide_height, 
						station        = default_station, 
						tides_filename = "NOAA_tides.json"):
	NOAA_tides = NOAA_checker(tide_height,station)
	store_tides(NOAA_tides)
	return NOAA_tides
def minutes_left_high_tide(tide_cutoff = default_tide_height):
	NOAA_tides = get_stored_tides()
	current_time = datetime.utcnow()
	for datapoint in NOAA_tides:
		time_string = datapoint['t']
		time = datetime.fromisoformat(time_string)
		tide = datapoint['v']
		if time > current_time:
			timedelta = time-current_time
			minutes_remaining = timedelta.seconds//60
			print(str(tide_cutoff) + " - " + str(tide))
			if float(tide) < tide_cutoff: high_tide = True
			else: high_tide = False
			return {'high_tide':high_tide,
					'time_remaining':minutes_remaining}
	print("No Tides Found In Storage. Re-Pinging NOAA")
	update_stored_tides(tide_cutoff)

update_stored_tides()
pprint(minutes_left_high_tide())

# if the  current time exceeds the last stored time, update stored tides and retry.

#pprint(NOAA_tides)


