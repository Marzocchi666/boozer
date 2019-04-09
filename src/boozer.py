#!/usr/bin/python
# -*- coding: utf-8 -*

import os
import pyfiglet 
import time
import math
import logging
#import scrollphat
# import RPi.GPIO as GPIO
from flowmeter import *
import beer_db
import twitter_notify
import slack_notify
import requests
import ConfigParser
import logging
import bar_mqtt
import time
import zope.event
from prettytable import PrettyTable
import os 
import sys
import socket
from contextlib import closing

# --=== Begin Rewrite
class Boozer:

	db = None
	config = None
	CONFIG_FILEPATH = "./config.ini"
	DB_FILEPATH = "./db.sqlite"
	MQTT_ENABLED = False
	TWITTER_ENABLED = False
	SCROLLPHAT_ENABLED = False
	SLACK_ENABLED = False
	TEMPERATURE_ENABLED = False
	scrollphat_cleared = True ## TODO: decouple this
	taps = []
	mqtt_client = None

	def __init__(self):
		# Setup the configuration
		self.config = ConfigParser.ConfigParser()
		self.config.read(self.CONFIG_FILEPATH)

		self.db = beer_db.BeerDB(self.DB_FILEPATH)  # TODO: replace this with configuration value
		#if config.get("Scrollphat", 'enabled') == "True":
		#    scrollphat.set_brightness(7)

		# Set the logger
		logger = logging.getLogger()
		handler = logging.StreamHandler()
		formatter = logging.Formatter(
			'%(asctime)s %(name)-12s %(levelname)-8s %(message)s')
		handler.setFormatter(formatter)
		logger.addHandler(handler)
		logger.setLevel(logging.DEBUG)
		logger.setLevel(logging.INFO)


		current_path = os.path.dirname(os.path.realpath(__file__))
		if not os.path.isfile(self.DB_FILEPATH):
			logger.fatal("[fatal] cannot load db from " % self.DB_FILEPATH)
			sys.exit(1)
		if not os.path.isfile(self.CONFIG_FILEPATH):
			logger.fatal("[fatal] cannot load config from " % self.CONFIG_FILEPATH)
			sys.exit(1)

		# setup twitter client
		try:
			if self.config.getboolean("Twitter", "enabled"):
				self.TWITTER_ENABLED = True
				twitter = twitter_notify.TwitterNotify(self.config)
		except: 
			logger.info("Twitter Entry not found in %s, setting TWITTER_ENABLED to False", sys.exc_info()[0] )
			TWITTER_ENABLED = False

		# setup mqtt client
		try:
			if self.config.getboolean("Mqtt", "enabled"):
				logger.info("config: MQTT enabled")
				self.MQTT_ENABLED = True
				self.mqtt_client = bar_mqtt.BoozerMqtt(self.config.get("Mqtt", "broker"), port=self.config.get("Mqtt", "port"))
		except: 
			logger.info("MQTT Entry not found in %s, setting MQTT_ENABLED to False" % self.CONFIG_FILEPATH)
			self.MQTT_ENABLED = False

		# setup temperaturesensor client
		try:
			if self.config.getboolean("Temperature", "enabled"):
				self.TEMPERATURE_ENABLED = True
				temperature_url = self.config.get("Temperature", "endpoint")
		except: 
			logger.info("Temperature Entry not found in %s, setting TEMPERATURE_ENABLED to False")
			self.TEMPERATURE_ENABLED = False

		# setup slack client
		try:
			if self.config.getboolean("Slack", "enabled"):
				self.SLACK_ENABLED = True
				slack = slack_notify.SlackNotify(self.config)
		except: 
			logger.info("Slack Entry not found in %s, setting SLACK_ENABLED to False")
			self.TEMPERATURE_ENABLED = False

		# set up the flow meters
		  
		for tap in range(1,10): # limit of 10 taps
			str_tap = "tap%i" % tap 
			str_tapN_gpio_pin = "%s_gpio_pin" % str_tap
			str_tapN_beer_name = "%s_beer_name" % str_tap
			str_tapN_reset_database = "%s_reset_database" % str_tap

			try:
				this_tap_gpio_pin = self.config.getint("Taps", str_tapN_gpio_pin) # this looks for the tap gpio pin such as "tap1_gpio_pin"
				this_tap_beer_name = [self.config.get("Taps", str_tapN_beer_name)]
				new_tap = FlowMeter("not metric", this_tap_beer_name, tap_id=tap, pin=this_tap_gpio_pin, config=self.config) # Create the tap object
				self.taps.append(new_tap) # Add the new tap object to the array
			except:
				break

			# If mqtt is enabled, we need to push the new value. This is because mqtt does not always persist and that's a good thing to do.
			if self.MQTT_ENABLED:
				self.update_mqtt(tap)

			# Check to see if we need to reset the database value
			try:
				if self.config.getboolean('Taps', str_tapN_reset_database):
					self.db.reset_tap_val(tap)
					logger.info("Detected %s. Successfully reset the database entry to 100 percent volume remaining." % str_tapN_reset_database)
			except:
				continue

		if len(self.taps) < 1:
			# if there were no taps read in, there's no point living anymore. go fatal
			logger.fatal("FATAL - No taps were read in from the config file. Are they formatted correctly?")
			sys.exit(1)
		## TODO This needs to be pulled into the init script 
		for tap in self.taps:  # setup all the taps. add event triggers to the opening of the taps.
			GPIO.add_event_detect(tap.get_pin(), GPIO.RISING, callback=lambda *a: self.register_tap(tap), bouncetime=20)
			#if MQTT_ENABLED: update_mqtt(tap.get_tap_id()) # do a prelim mqtt update in case it's been awhile

		# Initial info
		if self.TEMPERATURE_ENABLED:
			logger.info("Temperature: " + self.get_temperature())

		zope.event.subscribers.append(self.register_pour_event) # Attach the event

	def update_mqtt(self, tap_id):
		"""

		:param tap_id:
		:return:
		"""
		percent = self.db.get_percentage100(tap_id)
		topic = "bar/tap%s" % str(tap_id)
		try:
			self.mqtt_client.pub_mqtt(topic, str(percent))
		except:
			logger.error("Unable to publish mqtt update for tap: %i " % int(tap_id)    )
			logger.error(sys.exc_info()[0])

	# More config
	def is_port_open(self, host, port):
		import socket
		status = True
		
		with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
			sock.settimeout(2) 
			if sock.connect_ex(((host), (port))) == 0:
				status = False
		return status

	def get_enabled_string(self, val):
		if val == True:
			return "enabled"
		else:
			return "disabled"

	def print_config(self):
		result = pyfiglet.figlet_format("BOOZER") #, font = "slant" ) 
		print
		print
		print result

		files_table = PrettyTable(['File','Filepath', 'Exists'])
		files_table.add_row(['Database', self.DB_FILEPATH, os.path.isfile(self.DB_FILEPATH)])
		files_table.add_row(['Configuration', self.CONFIG_FILEPATH, os.path.isfile(self.CONFIG_FILEPATH)])
		print files_table

		t = PrettyTable(['Feature','Status'])
		t.add_row(['Twitter', self.get_enabled_string(self.TWITTER_ENABLED)])
		t.add_row(['Mqtt', self.get_enabled_string(self.MQTT_ENABLED)])
		t.add_row(['Temperature', self.get_enabled_string(self.TEMPERATURE_ENABLED)])
		t.add_row(['Slack', self.get_enabled_string(self.SLACK_ENABLED)])
		print t

		taps_table = PrettyTable(['Tap','Beer','GPIO Pin', 'Volume Remaining'])
		for tap in self.taps:
			taps_table.add_row([str(tap.get_tap_id()), str(tap.get_beverage_name()[0]), str(tap.get_pin()), str(self.db.get_percentage100(tap.get_tap_id()))])
		print taps_table

		if self.MQTT_ENABLED == True:    
			mqtt_host = self.config.get("Mqtt", "broker")
			mqtt_port = self.config.get("Mqtt", "port")
			mqtt_table = PrettyTable(['MQTT-Key','MQTT-Value'])
			mqtt_table.add_row(['Broker', str(mqtt_host)])
			mqtt_table.add_row(['Port', str(mqtt_port)])
			conn_str = "Connected"
			if self.is_port_open(host=mqtt_host, port=int(mqtt_port)):
				conn_str = "Unable to Connect"
			mqtt_table.add_row(['Connected?', conn_str])

			print mqtt_table


	def get_temperature(self):
		"""
		Parses a http GET request for the connected temperature sensor. Yes, this
		relies on an external sensor-serving process, I recommend https://github.com/bgulla/sensor2json

		:return: string
		"""
		if not TEMPERATURE_ENABLED:
			return None

		try:
			r = requests.get(self.config.get("Temperature", "endpoint"))
			if r.status_code == 200:
				return r.text
			else:
				return "error_http"
		except:
			return "error"


	def record_pour(self, tap_id, pour):
		self.db.update_tap(tap_id, pour)

	def update_display(self, msg):
		if SCROLLPHAT_ENABLED:
			self.update_scrollphat(msg)
			
	def update_scrollphat(self, msg):
		scrollphat.write_string(msg, 11)
		length = scrollphat.buffer_len()

		for i in range(length):
			try:
				scrollphat.scroll()
				time.sleep(0.1)
			except KeyboardInterrupt:
				scrollphat.clear()

	def register_tap(self, tap_obj):
		"""

		:param tap_obj:
		:return:
		"""
		currentTime = int(time.time() * FlowMeter.MS_IN_A_SECOND)
		tap_obj.update(currentTime)



	def register_pour_event(self, tap_obj ):
		tap_event_type = tap_obj.last_event_type

		if tap_event_type == FlowMeter.POUR_FULL:
			# we have detected that a full beer was poured
			register_new_pour(tap_obj)
		elif tap_event_type == FlowMeter.POUR_UPDATE:
			# it was just a mid pour 
			# TODO: Update scrollphat here
			logger.debug("flowmeter.POUR_UPDATE")
			#print "brandon do something like update the scrollphat display or do nothing. it's cool"



	def register_new_pour(self, tap_obj):
		"""
		"""
		
		pour_size = round(tap_obj.thisPour * tap_obj.PINTS_IN_A_LITER, 3)
				# receord that pour into the database
		
		try:
			self.db.update_tap(tap_obj.tap_id, pour_size) # record the pour in the db
			logger.info("Database updated: %s %s " % (str(tap_obj.tap_id), str(pour_size)))
		except :
			logger.error("unable to register new pour event to db")
		
		
		# calculate how much beer is left in the keg
		#volume_remaining = str(round(db.get_percentage(tap_obj.tap_id), 3) * 100)
		volume_remaining = str(self.db.get_percentage(tap_obj.tap_id))

		# is twitter enabled?
		if self.TWITTER_ENABLED:
			logger.info("Twitter is enabled. Preparing to send tweet.")
			# calculate how much beer is left in the keg
			# tweet of the record
			msg = twitter.tweet_pour(tap_obj.tap_id,
								tap_obj.getFormattedThisPour(),
								tap_obj.getBeverage(),
								volume_remaining,
								temperature=get_temperature())  # TODO make temperature optional
			logger.info("Tweet Sent: %s" % msg)
			#if SCROLLPHAT_ENABLED : scroll_once(msg)

		if self.SLACK_ENABLED:
			logger.info("Slack notifications are enabled. Preparing to send slack update.")
			
			# tweet of the record
			msg = slack.slack_pour(tap_obj.tap_id,
								tap_obj.getFormattedThisPour(),
								tap_obj.getBeverage(),
								volume_remaining,
								self.get_temperature())  # TODO make temperature optional
			logger.info("Sent slack update: %s" % msg)
		# reset the counter
		tap_obj.thisPour = 0.0
		logger.info("reseting pour amount to 0.0")

		# publish the updated value to mqtt broker
		if config.getboolean("Mqtt", "enabled"): update_mqtt(tap_obj.tap_id)

		# display the pour in real time for debugging
		if tap_obj.thisPour > 0.05: logger.debug("[POUR EVENT] " + str(tap_obj.tap_id) + ":" + str(tap_obj.thisPour))

	def run(self):
		# --- Begin old main
		self.print_config()
		logger.info("Boozer Intialized! Waiting for pours. Drink up, be merry!")
		while True:

			# Handle keyboard events
			currentTime = int(time.time() * FlowMeter.MS_IN_A_SECOND)
			
			for tap in self.taps:
				tap.listen_for_pour()

			# go night night
			time.sleep(0.01)

def main():
	boozer = Boozer()
	boozer.run()


if __name__ == "__main__":
	main()

