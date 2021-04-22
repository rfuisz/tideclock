import urllib.parse
import urllib.request
import json
from datetime import datetime
from datetime import timedelta

from pprint import pprint

#assumes high tide is at the 12 o'clock position, and low tide is at the 6 o'clock position.

default_tide_height = 2
default_station = '9414290'
default_days_between_NOAA_updates = 7

def NOAA_checker(tide_cutoff=default_tide_height, station = default_station):
	begin_date = datetime.utcnow()
	end_date   = begin_date + timedelta(days=default_days_between_NOAA_updates)
	begin_date_string = begin_date.strftime("%Y%m%d")
	end_date_string = end_date.strftime("%Y%m%d")
	api = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?"
	post_params =  {'begin_date': begin_date_string,
					'end_date'  : end_date_string,
					'station'   : str(station),
					'product'   : 'predictions',
					'datum'     : 'MLLW',
					'time_zone' : 'gmt',
					'units'     : 'english',
					'format'    : 'json'}
	post_args = urllib.parse.urlencode(post_params)
	api_url = api+post_args
	print("Tide cutoff: "+str(tide_cutoff))
	print("Pinging NOAA for Tides...")
	print(api_url)
	request = urllib.request.urlopen(api_url)
	print("NOAA Suggests these tides:")
	tide_predictions = json.load(request)['predictions']
	tide_cutoff = float(tide_cutoff)
	flood_and_ebb_tides = []
	for index, time_point in enumerate(tide_predictions):
		tide = float(time_point['v'])
		previous_tide = float(tide_predictions[index-1]['v'])
		time = str(time_point['t'])
		if tide > tide_cutoff and previous_tide < tide_cutoff:
			print("flood tide "+str(tide) + " at time " + time)
			flood_and_ebb_tides.append(time_point)
		elif tide < tide_cutoff and previous_tide > tide_cutoff:
			flood_and_ebb_tides.append(time_point)
			print("ebb tide "+ str(tide) + " at time " + time)
	return flood_and_ebb_tides
def store_tides(tides, tides_filename ="NOAA_tides.json" ):
	tides_json = {'metadata': {
					'default_station' : default_station,
					'default_tide_height' : default_tide_height,},
				'tides': tides}
	f = open(tides_filename, "w")
	f.write(json.dumps(tides_json, indent = 2))
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
	NOAA_tides = check_defaults_get_stored_tides()['tides']
	current_time = datetime.utcnow()
	for datapoint in NOAA_tides:
		time_string = datapoint['t']
		time = datetime.fromisoformat(time_string)
		tide = datapoint['v']
		if time > current_time:
			timedelta = time-current_time
			minutes_remaining = timedelta.seconds//60
			if float(tide) < tide_cutoff: high_tide = True
			else: high_tide = False
			return {'high_tide':high_tide,
					'time_remaining':minutes_remaining}
	print("No Tides Found In Storage. Re-Pinging NOAA")
	update_stored_tides(tide_cutoff) # if the  current time exceeds the last stored time, update stored tides and retry.
def tide_to_degrees(current_tide, 
					high_tide_clockface_minutes = (6*60), 
					low_tide_clockface_minutes  = (6*60)):
	time_to_switch = current_tide['time_remaining']
	if current_tide['high_tide']: 
		clockface_minutes = high_tide_clockface_minutes
		degrees = 0
	else: 
		clockface_minutes =  low_tide_clockface_minutes
		degrees = 180
	time_to_switch = min(current_tide['time_remaining'], clockface_minutes)
	degrees_per_minute = 180/(clockface_minutes)
	degrees += degrees_per_minute * ((clockface_minutes) - time_to_switch)
	return degrees
def check_defaults_get_stored_tides(tides_filename = "NOAA_tides.json"):
	stored_tides = get_stored_tides(tides_filename)
	metadata = stored_tides['metadata']
	if metadata['default_station'] == default_station and metadata['default_tide_height'] == default_tide_height:
		return stored_tides
	else:
		update_stored_tides(tides_filename = tides_filename)
		return get_stored_tides(tides_filename)


def get_tide_degrees():
	return tide_to_degrees(minutes_left_high_tide())

pprint(minutes_left_high_tide())
#pprint(get_tide_degrees())


#pprint(NOAA_tides)


