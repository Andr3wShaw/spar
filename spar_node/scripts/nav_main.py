#!/usr/bin/env python3
from argparse import ArgumentError
import argparse
import sys
import runpy
from math import *
import time

import rospy
import actionlib
from actionlib_msgs.msg import GoalStatus

from geometry_msgs.msg import Point, PoseStamped

from spar_msgs.msg import FlightMotionAction, FlightMotionGoal

from std_msgs.msg import String, Int32

from sensor_msgs.msg import BatteryState

from nav_msgs.msg import Path

# Libraries for interfacing with breadcrumb
from breadcrumb.srv import RequestPath
from breadcrumb.srv import RequestPathRequest

# This is getting a bit more complicated now, so we'll put our information in
# a class to keep track of all of our variables. This is not so much different
# to the previous methods, other than the fact that the class will operate
# within itself.
# i.e. it will have it's own publishers, subscribers, etc., that
# will call it's own functions as callbacks, etc.
class Guidance():
	def __init__(self, waypoints):
		
		#if self.waypointcounter


		#####################################################################
		# Set battery topic value (for simulator or actual mode)
		#self.topic_battery = "/uavasr/battery"
		self.topic_battery = "/mavros/battery"
		#self.topic_battery = "battery"
		#####################################################################

		self.critical_battery = 10 # Critical battery level

		self.count = 0
		# Make sure we have a valid waypoint list
		if not self.check_waypoints(waypoints):
			raise ArgumentError("Invalid waypoint list input!")
		
		# Internal counter to see what waypoint were are up to
		self.waypoint_counter = 0

		# Set a flag to indicate that we are doing a specific inspection
		# and that we are not following our waypoint list
		# This will stop our "waypoint is reached" callback from firing
		# during the roi diversion and taking over our flight!
		self.performing_roi = False
		self.safe_landing = False

		# Save the input waypoints
		self.waypoints = waypoints

		# Display the full path
		self.display_path(waypoints[0:floor(len(self.waypoints)/2)], "/mission_plan/path") #***

		#Initialise states of detected items
		self.person_detected = False
		self.backpack_detected = False
		self.landingID_detected = False
		self.search_area_complete = False

		self.mission_complete = False

		self.horizontal_offset = 0
		self.vertical_offset = 0
		# Make some space to record down our current location
		self.current_location = Point()
		self.landing_location = self.current_location

		# Set our linear and rotational velocities for the flight
		self.vel_linear = rospy.get_param("~vel_linear", 1)
		self.vel_yaw = rospy.get_param("~vel_yaw", 0.2)
		# Set our position and yaw waypoint accuracies
		self.accuracy_pos = rospy.get_param("~acc_pos", 0.3)
		self.accuracy_yaw = rospy.get_param("~acc_yaw", 0.3)

		# Create our action client
		action_ns = rospy.get_param("~action_topic", 'spar/flight')
		self.spar_client = actionlib.SimpleActionClient(action_ns, FlightMotionAction)
		rospy.loginfo("Waiting for spar...")
		self.spar_client.wait_for_server()

		# Wait to connect with Breadcrumb
		# Code will error if you try to connect to a service that does not exist
		rospy.wait_for_service('/breadcrumb/request_path')
		self.srvc_bc = rospy.ServiceProxy('/breadcrumb/request_path', RequestPath)
		rospy.loginfo("if code got stuck at this point is probably just breadcrumb not running")


		if not rospy.is_shutdown():
			# Good to go, start mission
			self.send_flight_motion()	#takeoff

			rospy.loginfo("Starting waypoint mission")

			# Setup first waypoint segment
			# XXX:	Another option would be to do "takeoff" and leave "waypoint_counter = 0" to
			#		begin the mission at the first waypoint after take-off
			self.send_wp(self.waypoints[0])
			self.waypoint_counter += 1

			# Initialisation breadcrumb wps ##################################
			self.breadcrumbWPSnextWP = 0
			self.breadcrumbMode = False
			self.breadcrumbWPS = [] ##########################################

			# Setup a timer to check if our waypoint has completed at 20Hz
			self.timer = rospy.Timer( rospy.Duration(1.0/20.0), self.check_waypoint_status )
			# Callback to save "current location" such that we can perform and return
			# from a diversion to the correct location
			# XXX: These topics could be hard-coded to avoid using a launch file
			self.sub_pose = rospy.Subscriber("~pose", PoseStamped, self.callback_pose)
			# Subscriber to catch "ROI" diversion commands
			#self.sub_roi = rospy.Subscriber("~roi", PoseStamped, self.callback_inspect_roi)
			# Subscriber to monitor battery
			self.sub_battery = rospy.Subscriber(self.topic_battery, BatteryState, self.battery_callback)
			# Subscribe to Object Detection
			self.sub_object = rospy.Subscriber("/object_detection", String, self.detected_object)

			self.pub_deploy = rospy.Publisher('deploy_payload', Int32, queue_size=10)
			self.pub_aruco_pose = rospy.Publisher('aruco_pose_shifted', PoseStamped, queue_size=10)
			self.pub_object_pose = rospy.Publisher('object_pose_shifted', PoseStamped, queue_size=10)

			self.sub_aruco = rospy.Subscriber('/aruco_marker/id', Int32, self.aruco_detection)
			self.aruco_pose_sub = rospy.Subscriber('/aruco_pose', Point, self.offset_calculation)
			self.sub_object_pose = rospy.Subscriber('object_pose', Point, self.offset_calculation)

			self.sub_position = rospy.Subscriber('mavros/local_position/pose', PoseStamped, self.callback_pose) 
			self.sub_position = rospy.Subscriber('uavasr/pose', PoseStamped, self.callback_pose)

			# self.sub_aruco_pose = rospy_Subscrber('/aruco_pose', Int32, self.aruco)


			# Publisher to GCS
			#self.pub_battery_warning = rospy.Subscriber("critical_battery", String, queue_size=10)#***


			# XXX: Could have a publisher to output our waypoint progress
			# throughout the flight (should publish each time the waypoint
			# counter is increased). Note: will also need to import "Float32"
			# from "std_msgs.msg" in the header
			# self.pub_progress = rospy.Subscriber("~waypoint_progress", Float32, 10)

			# If shutdown is issued (eg. CTRL+C), cancel current
	 		# mission before rospy is shutdown.
			rospy.on_shutdown( lambda : self.shutdown() )

	# This function will check if a list of waypoints is in the format we expect
	def check_waypoints(self, wps):
		# Make sure waypoints are a list
		if not isinstance(wps, list):
			rospy.logwarn("Waypoints are not list")
			return False

		# Make sure we have at least one waypoint
		if len(wps) < 1:
			rospy.logwarn("Waypoints list is empty")
			return False

		# Check each of our waypoints
		for i in range(len(wps)):
			if not self.check_waypoint(wps[i]):
				rospy.logwarn("Waypoint %i did not pass check" % (i + 1))
				return False

		# If we haven't returned false yet, then waypoints look good!
		return True


	# This function will check if a waypoint is in the format we expect
	def check_waypoint(self, wp):
		# Make sure each waypoint is a list
		if not isinstance(wp, list):
			rospy.logwarn("Waypoint is not a list of coordinates")
			return False

		# Make sure each waypoint has 4 values
		if len(wp) != 4:
			rospy.logwarn("Waypoint has an invalid length (must be X/Y/Z/Yaw)")
			return False

		# If we haven't returned false yet, then waypoint looks valid!
		return True


	# This function will make sure we shut down the node as safely as possible
	def shutdown(self):
		# Unregister anything that needs it here
	
		self.sub_pose.unregister()
		self.sub_roi.unregister()
		self.spar_client.cancel_goal()

		rospy.loginfo("Guidance stopped")


	# This function will check receive the current pose of the UAV constantly
	def callback_pose(self, msg_in):
		# Store the current position at all times so it can be accessed later
		self.current_location = msg_in.pose.position
		# self.current_location = self.landing_location
		# self.landing_location = [self.landing_location.x, self.landing_location.y, self.landing_location.z, 0]

		
	def aruco_detection(self, msg_in):
		global ID
		if self.landingID_detected == False:
			self.landing_location = self.current_location

			self.landing_location = [self.landing_location.x + self.horizontal_offset, self.landing_location.y + self.vertical_offset, self.landing_location.z, 0]
			rospy.loginfo("ArUco Detected: %d", msg_in.data)
			# self.pub_aruco_pose.publish(self.landing_location)
			print(msg_in.data) #NOT WORKING
			if int(msg_in.data) == ID: 
				self.landingID_detected = True
				print ("Landing Location Saved")
		else:
			return
		


	def offset_calculation(self, msg_in):
		global altitude
		horizontal_pixels = msg_in.y
		vertical_pixels = msg_in.x

		self.horizontal_offset = ((horizontal_pixels/416) * 2 * altitude * tan(19.09)) - altitude * tan(19.09)
		self.vertical_offset = ((vertical_pixels/416) * 2 * altitude * tan(19.09)) - altitude * tan(19.09) - 0.1
		rospy.loginfo(self.horizontal_offset)
		rospy.loginfo(self.vertical_offset)

	def mission_complete_check(self):
		global wps_index
		global wps_all

		if self.waypoint_counter == len(wps_all[wps_index]):
			rospy.loginfo('testing search area complete')
			self.search_area_complete = True

		if self.backpack_detected == True and self.person_detected == True and self.landingID_detected == True and self.search_area_complete == True:
			self.mission_complete = True
		
		# if (self.backpack_detected == False or self.person_detected == False or self.landingID_detected == False) and self.search_area_complete == True:
		# 	self.search_area_complete = False
		# 	rospy.loginfo('testing restart mission')

	def detected_object(self, msg_in):
		print(msg_in.data)
		if self.waypoint_counter >= 1:
			
			self.performing_roi = True
			self.spar_client.cancel_goal()

			current_location = self.current_location
			current_location = [self.current_location.x, self.current_location.y, self.current_location.z, 0]


			deployment_position = self.current_location
			deployment_position.z = 1
			actual_position = [deployment_position.x + self.horizontal_offset, deployment_position.y + self.vertical_offset, deployment_position.z, 0]
			# self.pub_object_pose.publish(actual_position)
			rospy.loginfo(f"Deployment Position: x={deployment_position.x}, y={deployment_position.y}, z={deployment_position.z}")
			# Print actual_position
			rospy.loginfo(f"Actual Position: x={actual_position[0]}, y={actual_position[1]}, z={actual_position[2]}, w={actual_position[3]}")

			
			if msg_in.data == "Person" and self.person_detected == False:
				#rospy.loginfo(msg_in.data)
				self.person_detected = True
				#rospy.Publisher("object_detection", String, queue_size=10)
				self.send_wp(actual_position)
				self.spar_client.wait_for_result()
				rospy.sleep(4)
				self.pub_deploy.publish(0)
				rospy.sleep(1)
			if msg_in.data == "Backpack" and self.backpack_detected == False:
				#rospy.loginfo(msg_in.data)
				self.backpack_detected = True
				#rospy.Publisher("object_detection", String, queue_size=10)
				self.send_wp(actual_position)
				self.spar_client.wait_for_result()
				rospy.sleep(4)
				self.pub_deploy.publish(1)
				rospy.sleep(1)
			
			self.send_wp(current_location)
			self.spar_client.wait_for_result()
			if self.spar_client.get_state() != GoalStatus.SUCCEEDED:
				# Something went wrong, cancel out of guidance!
				rospy.signal_shutdown("cancelled")
				return

			self.send_wp(self.waypoints[self.waypoint_counter])
			self.performing_roi = False
		else:
			return

	# This function will fire whenever a ROI pose message is sent
	# It is also responsible for handling the ROI "inspection task"
	# def callback_inspect_roi(self, msg_in):
	# 	# Set our flag that we are performing the diversion
	# 	self.performing_roi = True

	# 	rospy.loginfo("Starting diversion to ROI...")
	# 	# Cancel the current goal (if there is one)
	# 	self.spar_client.cancel_goal()
	# 	# Record our current location so we can return to it later
	# 	start_location = self.current_location
	# 	# XXX:	It would also be a good idea to capture "current yaw" from
	# 	#		the pose to maintain that throughout a diversion

	# 	# Set the "diversion waypoint" (at yaw zero)
	# 	dwp = [msg_in.pose.position.x, msg_in.pose.position.y, msg_in.pose.position.z, 0.0]
	# 	# Set the "return waypoint" (at yaw zero)
	# 	rwp = [self.current_location.x, self.current_location.y, self.current_location.z, 0.0]

	# 	# XXX: Could pause here for a moment with ( "rospy.sleep(...)" ) to make sure the UAV stops correctly

	# 	self.send_wp(dwp)
	# 	self.spar_client.wait_for_result()
	# 	if self.spar_client.get_state() != GoalStatus.SUCCEEDED:
	# 		# Something went wrong, cancel out of guidance!
	# 		rospy.signal_shutdown("cancelled")
	# 		return

	# 	rospy.loginfo("Reached diversion ROI!")
	# 	# XXX: Do something?
	# 	rospy.sleep(rospy.Duration(10))

	# 	rospy.loginfo("Returning to flight plan...")

	# 	self.send_wp(rwp)
	# 	self.spar_client.wait_for_result()
	# 	if self.spar_client.get_state() != GoalStatus.SUCCEEDED:
	# 		# Something went wrong, cancel out of guidance!
	# 		rospy.signal_shutdown("cancelled")
	# 		return


	# 	# "waypoint_counter" represents the "next waypoint"
	# 	# "waypoint_counter - 1" represents the "current waypoint"
	# 	rospy.loginfo("Resuming flight plan from waypoint %i!" % (self.waypoint_counter - 1))
	# 	self.send_wp(self.waypoints[self.waypoint_counter - 1])
	# 	# Unset our flag that we are performing a diversion
	# 	# to allow the waypoint timer to take back over
	# 	self.performing_roi = False


	# This function is for convinience to simply send out a new waypoint
	def send_wp(self, wp):
		# Make sure the waypoint is valid before continuing
		# if not self.check_waypoint(wp):
		# 	raise ArgumentError("Invalid waypoint input!")
		# Build the flight goal
		goal = FlightMotionGoal()
		goal.motion = FlightMotionGoal.MOTION_GOTO
		goal.position.x = wp[0]
		goal.position.y = wp[1]
		goal.position.z = wp[2]
		goal.yaw = wp[3]
		goal.velocity_vertical = self.vel_linear
		goal.velocity_horizontal = self.vel_linear
		goal.yawrate = self.vel_yaw
		goal.wait_for_convergence = True
		goal.position_radius = self.accuracy_pos
		goal.yaw_range = self.accuracy_yaw

		# For this function, we don't wait in the loop.
		# Instead we just send the waypoint and check up on it later
		# This checking is either with the "self.timer" for waypoints
		# or with direct calls during the ROI diversion
		self.spar_client.send_goal(goal)
		# self.count += 1
		# rospy.loginfo(self.count)
		# if self.waypoint_counter == 2:
		# 	rospy.sleep(rospy.Duration(2))
		# 	self.detected_object("Backpack")
		# if self.waypoint_counter == 3:
		# 	rospy.sleep(rospy.Duration(2))
		# 	self.detected_object("Person")

		 # If shutdown is issued, cancel current mission before rospy is shutdown
		rospy.on_shutdown(lambda : self.spar_client.cancel_goal())

	# This fuction will run to check battery level status
	def battery_callback(self, battery_info):
		self.battery_percent = battery_info.percentage # battery percentage is a float from 0 - 1
		self.battery_charge = battery_info.charge # charge is measured in Amp Hours

	#####################################################################################
	# Safe landing proceudure for UAV. exucutes when low battery percentage is detected.
	def execute_safe_landing(self, timer=None):
		# set flag indicating safe landing mode
		self.safe_landing = True
		# Cancel current goal
		self.spar_client.cancel_goal()

		# check battery level somewhere else and if battery low call this funtion (Maybe in send_wp)
		self.printAndSave("Battery level critical! Sarting safe landing")
		#rospy.Publisher("critical_battery", String, queue_size=10)
		#self.pub_battery_warning.publish("warning, critical battery level. Sarting safe landing")

		# Unsubscribe to everything
		self.shutdown()

		# Send landing location (SHOULD BE UPDATED TO DESIGNATED ARUCO POSITION)
		self.send_wp(self.landing_location)# <- should be updated to aruco detection function
		self.spar_client.wait_for_result()
		if self.spar_client.get_state() != GoalStatus.SUCCEEDED:
			# Something went wrong, cancel out of guidance!
			rospy.signal_shutdown("cancelled")
			return
		rospy.loginfo("Landing")
		self.send_landing_motion()	
	#####################################################################################

	# This function will fire whenever we recieve a timer event (te) from rospy.Timer()
	# The main purpose is to check if a waypoint has been reached,
	# and if so, send out the next waypoint to continue the mission
	def check_waypoint_status(self, te):
		#Update path to search again
		if self.waypoint_counter == floor(len(self.waypoints)/2):
			self.display_path(self.waypoints[floor(len(self.waypoints)/2):len(self.waypoints)], "/mission_plan/path")
		
		# If we're performing the ROI diversion, then don't do
		# anything here, as this is handled in that function
		if self.performing_roi == False:
			self.mission_complete_check()
			if self.mission_complete == True:
				# Else the mission is over, shutdown and quit the node
				# :	This could be used to restart the mission back to the
				#		first waypoint instead to restart the mission
				self.send_wp(self.landing_location)
				self.spar_client.wait_for_result()

				if self.spar_client.get_state() != GoalStatus.SUCCEEDED:
					# Something went wrong, cancel out of guidance!
					rospy.signal_shutdown("cancelled")
					return

				self.send_landing_motion()
				#rospy.loginfo("ArUco Marker" + msg_in.data)
				rospy.loginfo("Mission complete!")
				rospy.signal_shutdown("complete")
				# If the last segment has succeeded.
				# For more complex tasks, it might be necessary to also
				# check if you are in waypoint or diversion mode here.
				# Hint: really, we should check for other status states
				#		(such as aborted), as there are some states
				#		where we won't recover from, and should just exit
			if self.spar_client.get_state() == GoalStatus.SUCCEEDED:
				rospy.loginfo("Reached waypoint %i!" % (self.waypoint_counter))

				# XXX:	Another check could go here to finish the mission early
				#		if "all" inspection tasks have been completed
				#		(Add in another "if" and make the waypoint counter check
				#		 an "elif" check instead.
				#		 i.e. if complete; elif more wps; else wps finished)
				if self.waypoint_counter < (len(self.waypoints)):
					if not self.breadcrumbMode:
						# Set up a path request for breadcrumb
						req = RequestPathRequest() # define start and end points for A* path
						req.start.x = self.waypoints[self.waypoint_counter-1][0] # get X
						req.start.y = self.waypoints[self.waypoint_counter-1][1] # get Y
						req.start.z = self.waypoints[self.waypoint_counter-1][2] # get Z
						req.end.x = self.waypoints[self.waypoint_counter][0] # get new X
						req.end.y = self.waypoints[self.waypoint_counter][1] # get new Y
						req.end.z = self.waypoints[self.waypoint_counter][2] # get new Z

						res = self.srvc_bc(req) # breadcrumb solution

						# Breadcrumb will return a vector of poses if a solution was found
						# If no solution was found (i.e. no solution, or request bad
						# start/end), then breadcrumb returns and empty vector
						# XXX: You could also use res.path_sparse (see breadcrumb docs)

						breadcrumbWPS = [] # initialise breadcrumb path list ***

						if len(res.path_sparse.poses) > 0: # Check if solution was found
							# Print the path to the screen
							rospy.loginfo("Segment {}-1 to {}:".format(self.waypoint_counter, self.waypoint_counter))
							rospy.loginfo("[%0.2f;%0.2f;%0.2f] => [%0.2f;%0.2f;%0.2f]",
										req.start.x,req.start.y,req.start.z,
										req.end.x,req.end.y,req.end.z)
							
							# Loop through the solution returned from breadcrumb
							for i in range(len(res.path_sparse.poses)): # create breadcrumb wp list
								rospy.loginfo("    [%0.2f;%0.2f;%0.2f]",
											res.path_sparse.poses[i].position.x,
											res.path_sparse.poses[i].position.y,
											res.path_sparse.poses[i].position.z)
								breadcrumbWPS.append([res.path_sparse.poses[i].position.x, res.path_sparse.poses[i].position.y, res.path_sparse.poses[i].position.z, 0.0])#***

							# Display the braeacrumb path([])
							self.breadcrumbWPS = breadcrumbWPS
							self.display_path(breadcrumbWPS, "/guidance/breadcrumbPath") # might no work. double check 
							self.breadcrumbMode = True
							self.breadcrumbWPSnextWP = 0
							self.send_wp(self.breadcrumbWPS[self.breadcrumbWPSnextWP])
							self.breadcrumbWPSnextWP += 1 # increment breadcrumb wp counter

						else:
							rospy.logerr("solution not found")

					else:
						if self.breadcrumbWPSnextWP<(len(self.breadcrumbWPS)): # check if there breadcrumb wps left
							# execute breadcrumb path we just got
							self.send_wp(self.breadcrumbWPS[self.breadcrumbWPSnextWP])
							self.breadcrumbWPSnextWP += 1 # increment breadcrumb wp counter
						else:
							# If we are done with breadcrumb wps 
							self.waypoint_counter += 1 # Increment survey waypoint counter
							self.breadcrumbMode = False
			# 	else:
			# 		# Else the mission is over, shutdown and quit the node
			# 		# :	This could be used to restart the mission back to the
			# 		#		first waypoint instead to restart the mission
			# 		self.send_wp(self.landing_location)
			# 		self.spar_client.wait_for_result()
			# 		if self.spar_client.get_state() != GoalStatus.SUCCEEDED:
			# 			# Something went wrong, cancel out of guidance!
			# 			rospy.signal_shutdown("cancelled")
			# 			return

			# 		self.send_landing_motion()
			# #rospy.loginfo("ArUco Marker" + msg_in.data)
			# 		rospy.loginfo("Mission complete!")
			# 		rospy.signal_shutdown("complete")
			elif (self.spar_client.get_state() == GoalStatus.PREEMPTED) or (self.spar_client.get_state() == GoalStatus.ABORTED) or (self.spar_client.get_state() == GoalStatus.REJECTED):
				rospy.loginfo("Mission cancelled!")
				rospy.signal_shutdown("cancelled")


	def send_flight_motion(self):
		# Create our goal
		goal = FlightMotionGoal()
		goal.motion = FlightMotionGoal.MOTION_TAKEOFF
		goal.position.z = rospy.get_param("~height", 1.0)			# Other position information is ignored
		goal.velocity_vertical = rospy.get_param("~speed", 1.0)		# Other velocity information is ignored
		goal.wait_for_convergence = True							# Wait for our takeoff "waypoint" to be reached
		goal.position_radius = rospy.get_param("~position_radius", 0.3)
		goal.yaw_range = rospy.get_param("~yaw_range", 0.1)

		# Send the goal
		rospy.loginfo("Sending goal motion...")
		self.spar_client.send_goal(goal)
		# If shutdown is issued, cancel current mission before rospy is shutdown
		rospy.on_shutdown(lambda : self.spar_client.cancel_goal())
		# Wait for the result of the goal
		self.spar_client.wait_for_result()

		# Output some feedback for our flight
		result = self.spar_client.get_state()
		if result == GoalStatus.SUCCEEDED:
			rospy.loginfo("Take-off complete!")
		else:
			rospy.logerr("Take-off failed!")

			# Detailed Feedback
			if result != GoalStatus.SUCCEEDED:
				if(result == GoalStatus.PENDING) or (result == GoalStatus.ACTIVE):
					rospy.loginfo("Sent command to cancel current mission")
				elif(result == GoalStatus.PREEMPTED):
					rospy.logwarn("The current mission was cancelled")
				elif(result == GoalStatus.ABORTED):
					rospy.logwarn("The current mission was aborted")
				elif(result == GoalStatus.RECALLED):
					rospy.logerr("Error: The current mission was recalled")
				elif(result == GoalStatus.REJECTED):
					rospy.logerr("Error: The current mission was rejected")
				else:
					rospy.logerr("Error: An unknown goal status was recieved")

	def send_landing_motion(self):
		# Create our goal
		goal = FlightMotionGoal()
		goal.motion = FlightMotionGoal.MOTION_LAND
		goal.velocity_vertical = rospy.get_param("~speed", 0.2)		# Other velocity information is ignored
		# No other information is used

		# Send the goal
		rospy.loginfo("Sending goal motion...")
		self.spar_client.send_goal(goal)
		# If shutdown is issued, cancel current mission before rospy is shutdown
		rospy.on_shutdown(lambda : self.spar_client.cancel_goal())
		# Wait for the result of the goal
		self.spar_client.wait_for_result()

		# Output some feedback for our flight
		result = self.spar_client.get_state()
		if result == GoalStatus.SUCCEEDED:
			rospy.loginfo("Landing complete!")
		else:
			rospy.logerr("Landing failed!")

			# Detailed Feedback
			if result != GoalStatus.SUCCEEDED:
				if(result == GoalStatus.PENDING) or (result == GoalStatus.ACTIVE):
					rospy.loginfo("Sent command to cancel current mission")
				elif(result == GoalStatus.PREEMPTED):
					rospy.logwarn("The current mission was cancelled")
				elif(result == GoalStatus.ABORTED):
					rospy.logwarn("The current mission was aborted")
				elif(result == GoalStatus.RECALLED):
					rospy.logerr("Error: The current mission was recalled")
				elif(result == GoalStatus.REJECTED):
					rospy.logerr("Error: The current mission was rejected")
				else:
					rospy.logerr("Error: An unknown goal status was recieved")

	def display_path(self, wps, name):
		#def display_path(wps):
		rospy.loginfo("Diplaying braeacrumb path")
		#pub_path = rospy.Publisher("/mission_plan/path", Path, queue_size=10, latch=True)
		pub_path = rospy.Publisher(name, Path, queue_size=10, latch=True)
		msg = Path()
		msg.header.frame_id = "/map"
		msg.header.stamp = rospy.Time.now()

		for wp in wps:
			pose = PoseStamped()
			pose.pose.position.x = wp[0]
			pose.pose.position.y = wp[1]
			pose.pose.position.z = wp[2]

			pose.pose.orientation.w = 1.0
			pose.pose.orientation.x = 0.0
			pose.pose.orientation.y = 0.0
			pose.pose.orientation.z = 0.0

			msg.poses.append(pose)
		
		rospy.loginfo("Publishing breadcrumb path")
		pub_path.publish(msg)



def main():
    # Define Input for Desired Altitude of Flight 
    # parser = argparse.ArgumentParser(description='Run drone guidance script')
    # parser.add_argument('--altitude', type=float, default=3.5, help='Altitude for drone takeoff (default: 3.5)')
    # parser.add_argument('--ID', type=int, default = 1, help='Desired landing location aruco marker ID')
    # args = parser.parse_args()

	#initialise node
    rospy.init_node('guidance')
    global wps_index
    global altitude
    global ID
    global wps_all
    altitude = rospy.get_param('~altitude', 3.5)
    ID = rospy.get_param('~ID', 1)

	#Warn the user of an incorrect altitude input and abort flight
    if altitude > 4:
        rospy.logerr("Altitude Error: Too Large!")
        return
    elif altitude < 1.5: 
        rospy.logerr("Altitude Error: Too Small!")
        return

		#Warn the user of an incorrect ID input and abort flight
    if ID > 100:
        rospy.logerr("Landing Marker ID is too large")
        return
    elif ID < 0: 
        rospy.logerr("Landing Marker ID must be a postive number")
        return

    half_fov = altitude * tan(19.09)  # Calculate half field of view angle

    wp_3lanes = [
        [-4 + half_fov, -2.07, altitude, 0.0],
        [4 - half_fov, -2.07, altitude, 0.0],
        [4 - half_fov, 0, altitude, 0.0],
        [-4 + half_fov, 0, altitude, 0.0],
        [-4 + half_fov, 2.07, altitude, 0.0],
        [4 - half_fov, 2.07, altitude, 0.0]
		]

    wp_4lanes = [
        [-4 + half_fov, -2.325, altitude, 0.0],
        [4 - half_fov, -2.325, altitude, 0.0],
        [4 - half_fov, -0.775, altitude, 0.0],
        [-4 + half_fov, -0.775, altitude, 0.0],
        [-4 + half_fov, 0.775, altitude, 0.0],
        [4 - half_fov, 0.775, altitude, 0.0],
        [4 - half_fov, 2.325, altitude, 0.0],
        [-4 + half_fov, 2.325, altitude, 0.0]
		]

    wp_5lanes = [
        [-4 + half_fov, -2.48, altitude, 0.0],
        [4 - half_fov, -2.48, altitude, 0.0],
        [4 - half_fov, -1.24, altitude, 0.0],
        [-4 + half_fov, -1.24, altitude, 0.0],
        [-4 + half_fov, 0, altitude, 0.0],
        [4 - half_fov, 0, altitude, 0.0],
        [4 - half_fov, 1.24, altitude, 0.0],
        [-4 + half_fov, 1.24, altitude, 0.0],
        [-4 + half_fov, 2.48, altitude, 0.0],
        [4 - half_fov, 2.48, altitude, 0.0]
		]
	
    wp_6lanes = [
    	[-4 + half_fov, -2.58, altitude, 0.0],
    	[4 - half_fov, -2.58, altitude, 0.0],
        [4 - half_fov, -1.55, altitude, 0.0],
        [-4 + half_fov, -1.55, altitude, 0.0],
        [-4 + half_fov, -0.51, altitude, 0.0],
        [4 - half_fov, -0.51, altitude, 0.0],
        [4 - half_fov, 0.51, altitude, 0.0],
        [-4 + half_fov, 0.51, altitude, 0.0],
        [-4 + half_fov, 1.55, altitude, 0.0],
        [4 - half_fov, 1.55, altitude, 0.0],
		[4 - half_fov, 2.58, altitude, 0.0],
        [-4 + half_fov, 2.58, altitude, 0.0]
	]

    wp_7lanes = [
		[-4 + half_fov, -2.5, altitude, 0.0],
        [4 - half_fov, -2.5, altitude, 0.0],
        [4 - half_fov, -1.66, altitude, 0.0],
        [-4 + half_fov, -1.66, altitude, 0.0],
        [-4 + half_fov, -0.833, altitude, 0.0],
        [4 - half_fov, -0.833, altitude, 0.0],
        [4 - half_fov, 0.0, altitude, 0.0],
        [-4 + half_fov, 0.0, altitude, 0.0],
        [-4 + half_fov, 0.833, altitude, 0.0],
        [4 - half_fov, 0.833, altitude, 0.0],
		[4 - half_fov, 1.66, altitude, 0.0],
        [-4 + half_fov, 1.66, altitude, 0.0],
		[-4 + half_fov, 2.5, altitude, 0.0],
        [4 - half_fov, 2.5, altitude, 0.0]
	]
    
    wps_all = [wp_3lanes, wp_4lanes, wp_5lanes, wp_6lanes, wp_7lanes]
    #choose waypoints
    if altitude < 1.785: #boundary FOV condition		
        wps_index = 4
        wps = wps_all[wps_index] + wps_all[wps_index-1][::-1]
    elif altitude < 2.079:
        wps_index = 3
        wps = wps_all[wps_index] + wps_all[wps_index+1][::-1]
    elif altitude < 2.527:
        wps_index = 2
        wps = wps_all[wps_index] + wps_all[wps_index+1][::-1]
    elif altitude < 3.274:
        wps_index = 1
        wps = wps_all[wps_index] + wps_all[wps_index+1][::-1]
    else:
        wps_index = 0
        wps = wps_all[wps_index] + wps_all[wps_index+1][::-1]

	# Create our guidance class option
    guide = Guidance(wps)

    # Spin!
    rospy.spin()


if __name__ == '__main__':
    try:
        main()
    except rospy.ROSInterruptException:
        pass

print('')
