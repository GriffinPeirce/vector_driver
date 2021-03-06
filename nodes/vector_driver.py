#!/usr/bin/python3.6
# -*- encoding: utf-8 -*-
"""
This file implements an ANKI Vector ROS driver.

It wraps up several functionality of the Vector SDK including
camera and motors. As some main ROS parts are not python3.5
compatible, the famous "transformations.py" is shipped next
to this node. Also the TransformBroadcaster is taken from
ROS tf ones.

Copyright {2016} {Takashi Ogura}
Copyright {2017} {Peter Rudolph}
Copyright {2019} {Griffin Peirce}

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

"""
# system
import sys
import numpy as np
from copy import deepcopy
from PIL import Image

# vector SDK
import anki_vector
from anki_vector.util import radians

# ROS
import rospy
from transformations import quaternion_from_euler
# from camera_info_manager import CameraInfoManager

# ROS msgs
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from tf2_msgs.msg import TFMessage
from nav_msgs.msg import Odometry
from geometry_msgs.msg import (
    Twist,
    TransformStamped
)
from std_msgs.msg import (
    String,
    Float64,
    ColorRGBA,
    Int16,
)
from sensor_msgs.msg import (
    Image,
    CameraInfo,
    BatteryState,
    Range,
    Imu,
    JointState,
)


# reused as original is not Python3 compatible
class TransformBroadcaster(object):
    """
    :class:`TransformBroadcaster` is a convenient way to send transformation updates on the ``"/tf"`` message topic.
    """

    def __init__(self, queue_size=100):
        self.pub_tf = rospy.Publisher("/tf", TFMessage, queue_size=queue_size)

    def send_transform(self, translation, rotation, time, child, parent):
        """
        :param translation: the translation of the transformation as a tuple (x, y, z)
        :param rotation: the rotation of the transformation as a tuple (x, y, z, w)
        :param time: the time of the transformation, as a rospy.Time()
        :param child: child frame in tf, string
        :param parent: parent frame in tf, string

        Broadcast the transformation from tf frame child to parent on ROS topic ``"/tf"``.
        """

        t = TransformStamped()
        t.header.frame_id = parent
        t.header.stamp = time
        t.child_frame_id = child
        t.transform.translation.x = translation[0]
        t.transform.translation.y = translation[1]
        t.transform.translation.z = translation[2]

        t.transform.rotation.x = rotation[0]
        t.transform.rotation.y = rotation[1]
        t.transform.rotation.z = rotation[2]
        t.transform.rotation.w = rotation[3]

        self.send_transform_message(t)

    def send_transform_message(self, transform):
        """
        :param transform: geometry_msgs.msg.TransformStamped
        Broadcast the transformation from tf frame child to parent on ROS topic ``"/tf"``.
        """
        tfm = TFMessage([transform])
        self.pub_tf.publish(tfm)


class VectorRos(object):
    """
    The Vector ROS driver object.

    """
    
    def __init__(self, vec):
        """

        :type   vec:    anki_vector.Robot
        :param  vec:    The vector SDK robot handle (object).
        
        """

        # vars
        self._vector = vec
        self._lin_vel = .0
        self._ang_vel = .0
        self._cmd_lin_vel = .0
        self._cmd_ang_vel = .0
        self._last_pose = self._vector.pose
        self._wheel_vel = (0, 0)
        self._optical_frame_orientation = quaternion_from_euler(-np.pi/2., .0, -np.pi/2.)
        # self._camera_info_manager = CameraInfoManager('vector_camera', namespace='/vector_camera')

        # tf
        self._tfb = TransformBroadcaster()

        # params
        self._odom_frame = rospy.get_param('~odom_frame', 'odom')
        self._footprint_frame = rospy.get_param('~footprint_frame', 'base_footprint')
        self._base_frame = rospy.get_param('~base_frame', 'base_link')
        self._head_frame = rospy.get_param('~head_frame', 'head_link')
        self._camera_frame = rospy.get_param('~camera_frame', 'camera_link')
        self._camera_optical_frame = rospy.get_param('~camera_optical_frame', 'vector_camera')
        # camera_info_url = rospy.get_param('~camera_info_url', '')

        # pubs
        self._joint_state_pub = rospy.Publisher('joint_states', JointState, queue_size=1)
        self._odom_pub = rospy.Publisher('odom', Odometry, queue_size=1)
        self._imu_pub = rospy.Publisher('imu', Imu, queue_size=1)
        self._battery_pub = rospy.Publisher('battery', BatteryState, queue_size=1)
        self._touch_pub = rospy.Publisher('touch', Int16, queue_size=1)
        self._laser_pub = rospy.Publisher('laser', Range, queue_size=50)
        # Note: camera is published under global topic (preceding "/")
        self._image_pub = rospy.Publisher('/vector_camera/image', Image, queue_size=10)
        # self._camera_info_pub = rospy.Publisher('/vector_camera/camera_info', CameraInfo, queue_size=10)

        # subs
        # self._backpack_led_sub = rospy.Subscriber(
        #     'backpack_led', ColorRGBA, self._set_backpack_led, queue_size=1)
        self._twist_sub = rospy.Subscriber('cmd_vel', Twist, self._twist_callback, queue_size=1)
        self._say_sub = rospy.Subscriber('say', String, self._say_callback, queue_size=1)
        self._head_sub = rospy.Subscriber('head_angle', Float64, self._move_head, queue_size=1)
        self._lift_sub = rospy.Subscriber('lift_height', Float64, self._move_lift, queue_size=1)

        # diagnostics
        self._diag_array = DiagnosticArray()
        self._diag_array.header.frame_id = self._base_frame
        diag_status = DiagnosticStatus()
        diag_status.hardware_id = 'Vector Robot'
        diag_status.name = 'Vector Status'
        diag_status.values.append(KeyValue(key='Battery Voltage', value=''))
        diag_status.values.append(KeyValue(key='Head Angle', value=''))
        diag_status.values.append(KeyValue(key='Lift Height', value=''))
        self._diag_array.status.append(diag_status)
        self._diag_pub = rospy.Publisher('/diagnostics', DiagnosticArray, queue_size=1)

        # camera info manager
        # self._camera_info_manager.setURL(camera_info_url)
        # self._camera_info_manager.loadCameraInfo()

    def _publish_diagnostics(self):
        # alias
        diag_status = self._diag_array.status[0]

        # fill diagnostics array
        battery_voltage = self._vector.get_battery_state().battery_volts
        diag_status.values[0].value = '{:.2f} V'.format(battery_voltage)
        diag_status.values[1].value = '{:.2f} rad'.format(self._vector.head_angle_rad)
        diag_status.values[2].value = '{:.2f} mm'.format(self._vector.lift_height_mm)
        if battery_voltage > 3.8:
            diag_status.level = DiagnosticStatus.OK
            diag_status.message = 'Everything OK!'
        elif battery_voltage > 3.6:
            diag_status.level = DiagnosticStatus.WARN
            diag_status.message = 'Battery low! Go charge soon!'
        else:
            diag_status.level = DiagnosticStatus.ERROR
            diag_status.message = 'Battery very low! Vector will power off soon!'

        # update message stamp and publish
        self._diag_array.header.stamp = rospy.Time.now()
        self._diag_pub.publish(self._diag_array)

    def _move_head(self, cmd):
        """
        Move head to given angle.
        
        :type   cmd:    Float64
        :param  cmd:    The message containing angle in degrees. [-22.0 - 45.0]
        
        """
        # action = self._vector.behavior.set_head_angle(radians(cmd.data * np.pi / 180.), duration=0.1,
        #                                     in_parallel=True)
        action = self._vector.behavior.set_head_angle(radians(cmd.data * np.pi / 180.), duration=0.1)
        # action.wait_for_completed()

    def _move_lift(self, cmd):
        """
        Move lift to given height.

        :type   cmd:    Float64
        :param  cmd:    A value between [0 - 1], the SDK auto
                        scales it to the according height.

        """
        # action = self._vector.behavior.set_lift_height(height=cmd.data,
        #                                      duration=0.2, in_parallel=True)
        action = self._vector.behavior.set_lift_height(height=cmd.data,
                                     duration=0.2)
        # action.wait_for_completed()

    def _set_backpack_led(self, msg):
        """
        Set the color of the backpack LEDs.

        :type   msg:    ColorRGBA
        :param  msg:    The color to be set.

        """
        # setup color as integer values
        # color = [int(x * 255) for x in [msg.r, msg.g, msg.b, msg.a]]
        # create lights object with duration
        # light = cozmo.lights.Light(cozmo.lights.Color(rgba=color), on_period_ms=1000)
        # set lights
        # self._cozmo.set_all_backpack_lights(light)
        # TODO Griffin: Add backpack colour support once color instance is added to the anki_vector.robot class
        pass

    def _twist_callback(self, cmd):
        """
        Set commanded velocities from Twist message.

        The commands are actually send/set during run loop, so delay
        is in worst case up to 1 / update_rate seconds.

        :type   cmd:    Twist
        :param  cmd:    The commanded velocities.

        """
        # compute differential wheel speed
        axle_length = 0.05  # 5cm
        self._cmd_lin_vel = cmd.linear.x
        self._cmd_ang_vel = cmd.angular.z
        rv = self._cmd_lin_vel + (self._cmd_ang_vel * axle_length * 0.5)
        lv = self._cmd_lin_vel - (self._cmd_ang_vel * axle_length * 0.5)
        self._wheel_vel = (lv*1000., rv*1000.)  # convert to mm / s

    def _say_callback(self, msg):
        """
        The callback for incoming text messages to be said.

        :type   msg:    String
        :param  msg:    The text message to say.

        """
        self._vector.say_text(msg.data).wait_for_completed()

    def _publish_objects(self):
        """
        Publish detected object as transforms between odom_frame and object_frame.

        """
        # TODO Griffin: Update to visible objects only based on api changes
        for obj in self._vector.world.all_objects:
            now = rospy.Time.now()
            x = obj.pose.position.x * 0.001
            y = obj.pose.position.y * 0.001
            z = obj.pose.position.z * 0.001
            q = (obj.pose.rotation.q1, obj.pose.rotation.q2, obj.pose.rotation.q3, obj.pose.rotation.q0)
            # TODO Griffin: Update to filter all object types
            self._tfb.send_transform(
                (x, y, z), q, now, 'cube_' + str(obj.object_id), self._odom_frame
            )

    def _publish_image(self):
        """
        Publish latest camera image as Image with CameraInfo.

        """
        # only publish if we have a subscriber
        if self._image_pub.get_num_connections() == 0:
            return

        # get latest image from vectors's camera
        camera_image = self._vector.camera.latest_image
        if camera_image is not None:
            # convert image to gray scale
            img = camera_image.convert('RGB')
            # 640,360 image size?
            # img = camera_image
            ros_img = Image()
            ros_img.encoding = 'rgb8'
            ros_img.width = img.size[0]
            ros_img.height = img.size[1]
            ros_img.step = ros_img.width
            ros_img.data = img.tobytes()
            ros_img.header.frame_id = 'vector_camera'
            # vector_time = camera_image.image_recv_time
            # ros_img.header.stamp = rospy.Time.from_sec(vector_time)
            ros_img.header.stamp = rospy.Time.now()
            # publish images and camera info
            self._image_pub.publish(ros_img)
            # camera_info = self._camera_info_manager.getCameraInfo()
            # camera_info.header = ros_img.header
            # self._camera_info_pub.publish(camera_info)

    def _publish_joint_state(self):
        """
        Publish joint states as JointStates.

        """
        # only publish if we have a subscriber
        if self._joint_state_pub.get_num_connections() == 0:
            return

        js = JointState()
        js.header.stamp = rospy.Time.now()
        js.header.frame_id = 'vector'
        js.name = ['head', 'lift']
        js.position = [self._vector.head_angle_rad,
                       self._vector.lift_height_mm * 0.001]
        js.velocity = [0.0, 0.0]
        js.effort = [0.0, 0.0]
        self._joint_state_pub.publish(js)

    def _publish_imu(self):
        """
        Publish inertia data as Imu message.

        """
        # only publish if we have a subscriber
        if self._imu_pub.get_num_connections() == 0:
            return

        imu = Imu()
        imu.header.stamp = rospy.Time.now()
        imu.header.frame_id = self._base_frame
        imu.orientation.w = self._vector.pose.rotation.q0
        imu.orientation.x = self._vector.pose.rotation.q1
        imu.orientation.y = self._vector.pose.rotation.q2
        imu.orientation.z = self._vector.pose.rotation.q3
        imu.angular_velocity.x = self._vector.gyro.x
        imu.angular_velocity.y = self._vector.gyro.y
        imu.angular_velocity.z = self._vector.gyro.z
        imu.linear_acceleration.x = self._vector.accel.x * 0.001
        imu.linear_acceleration.y = self._vector.accel.y * 0.001
        imu.linear_acceleration.z = self._vector.accel.z * 0.001
        self._imu_pub.publish(imu)

    def _publish_battery(self):
        """
        Publish battery as BatteryState message.

        """
        # only publish if we have a subscriber
        if self._battery_pub.get_num_connections() == 0:
            return

        battery = BatteryState()
        battery.header.stamp = rospy.Time.now()
        battery.voltage = self._vector.get_battery_state().battery_volts
        battery.present = True
        if self._vector.get_battery_state().is_on_charger_platform:
            battery.power_supply_status = BatteryState.POWER_SUPPLY_STATUS_CHARGING
        else:
            battery.power_supply_status = BatteryState.POWER_SUPPLY_STATUS_NOT_CHARGING
        self._battery_pub.publish(battery)

    def _publish_touch(self):
        """
        Publish raw backpack touch value

        """

        if self._touch_pub.get_num_connections() == 0:
            return

        touch = Int16()
        touch.data = self._vector.touch.last_sensor_reading.raw_touch_value

        self._touch_pub.publish(touch)

    def _publish_laser(self):
        """
        TODO Griffin: Add a transform from base_link to base_laser
        TODO: Modify the is valid condition if significant slope traversal object detection required.
        Publish filtered laser distance as Range message. Will publish NaN distance if
        scanner is blocked or robot in orientation not suited to horizontal navigation.

        See API for sensor range, fov etc
        https://developer.anki.com/vector/docs/generated/anki_vector.proximity.html

        """

        if self._laser_pub.get_num_connections() == 0:
            return

        now = rospy.Time.now()
        laser = Range()
        laser.header.frame_id = self._base_frame
        laser.header.stamp = now
        laser.radiation_type = 1 # IR laser
        laser.field_of_view = 0.436332 # 25 deg
        laser.min_range = 0.03 # 30mm
        laser.max_range = 1.5 # 300mm
        laser_reading = self._vector.proximity.last_sensor_reading
        if(laser_reading.is_valid):
            laser.range = self._vector.proximity.last_sensor_reading.distance.distance_mm/1000
        else:
            laser.range = float('nan')
        self._laser_pub.publish(laser)


    def _publish_odometry(self):
        """
        Publish current pose as Odometry message.

        """
        # only publish if we have a subscriber
        if self._odom_pub.get_num_connections() == 0:
            return

        now = rospy.Time.now()
        odom = Odometry()
        odom.header.frame_id = self._odom_frame
        odom.header.stamp = now
        odom.child_frame_id = self._footprint_frame
        odom.pose.pose.position.x = self._vector.pose.position.x * 0.001
        odom.pose.pose.position.y = self._vector.pose.position.y * 0.001
        odom.pose.pose.position.z = self._vector.pose.position.z * 0.001
        q = quaternion_from_euler(.0, .0, self._vector.pose_angle_rad)
        odom.pose.pose.orientation.x = q[0]
        odom.pose.pose.orientation.y = q[1]
        odom.pose.pose.orientation.z = q[2]
        odom.pose.pose.orientation.w = q[3]
        odom.pose.covariance = np.diag([1e-2, 1e-2, 1e-2, 1e3, 1e3, 1e-1]).ravel()
        odom.twist.twist.linear.x = self._lin_vel
        odom.twist.twist.angular.z = self._ang_vel
        odom.twist.covariance = np.diag([1e-2, 1e3, 1e3, 1e3, 1e3, 1e-2]).ravel()
        self._odom_pub.publish(odom)

    def _publish_tf(self, update_rate):

        # TODO Griffin: Update transforms with Vector measurements. Currently assumes old cozmo specs
        """
        Broadcast current transformations and update
        measured velocities for odometry twist.

        Published transforms:

        odom_frame -> footprint_frame
        footprint_frame -> base_frame
        base_frame -> head_frame
        head_frame -> camera_frame
        camera_frame -> camera_optical_frame

        """
        now = rospy.Time.now()
        x = self._vector.pose.position.x * 0.001
        y = self._vector.pose.position.y * 0.001
        z = self._vector.pose.position.z * 0.001

        # compute current linear and angular velocity from pose change
        # Note: Sign for linear velocity is taken from commanded velocities!
        # Note: The angular velocity can also be taken from gyroscopes!
        dist = np.sqrt((self._last_pose.position.x - self._vector.pose.position.x)**2
                       + (self._last_pose.position.y - self._vector.pose.position.y)**2
                       + (self._last_pose.position.z - self._vector.pose.position.z)**2) / 1000.0
        self._lin_vel = dist * update_rate * np.sign(self._cmd_lin_vel)
        self._ang_vel = -(self._last_pose.rotation.angle_z.radians - self._vector.pose.rotation.angle_z.radians)* update_rate

        # publish odom_frame -> footprint_frame
        q = quaternion_from_euler(.0, .0, self._vector.pose_angle_rad)
        self._tfb.send_transform(
            (x, y, 0.0), q, now, self._footprint_frame, self._odom_frame)

        # publish footprint_frame -> base_frame
        q = quaternion_from_euler(.0, -self._vector.pose_pitch_rad, .0)
        self._tfb.send_transform(
            (0.0, 0.0, 0.02), q, now, self._base_frame, self._footprint_frame)

        # publish base_frame -> head_frame
        q = quaternion_from_euler(.0, -self._vector.head_angle_rad, .0)
        self._tfb.send_transform(
            (0.02, 0.0, 0.05), q, now, self._head_frame, self._base_frame)

        # publish head_frame -> camera_frame
        self._tfb.send_transform(
            (0.025, 0.0, -0.015), (0.0, 0.0, 0.0, 1.0), now, self._camera_frame, self._head_frame)

        # publish camera_frame -> camera_optical_frame
        q = self._optical_frame_orientation
        self._tfb.send_transform(
            (0.0, 0.0, 0.0), q, now, self._camera_optical_frame, self._camera_frame)

        # store last pose
        self._last_pose = deepcopy(self._vector.pose)

    def run(self, update_rate=10):
        """
        Publish data continuously with given rate.

        :type   update_rate:    int
        :param  update_rate:    The update rate.

        """
        r = rospy.Rate(update_rate)
        while not rospy.is_shutdown():
            self._publish_tf(update_rate)
            self._publish_image()
            self._publish_objects()
            self._publish_joint_state()
            self._publish_imu()
            self._publish_battery()
            self._publish_touch()
            self._publish_laser()
            self._publish_odometry()
            self._publish_diagnostics()
            # send message repeatedly to avoid idle mode.
            # This might cause low battery soon
            # TODO improve this!
            self._vector.motors.set_wheel_motors(*self._wheel_vel)
            # sleep
            r.sleep()
        # stop movement
        self._vector.motors.set_wheel_motors((0, 0))


def vector_app(vec):
    """
    The main function of the vector ROS driver.

    This function is called by vector SDK!
    Use "vector.connect(vector_app)" to run.

    :type   vec_conn:   vector.Connection
    :param  coz_conn:   The connection handle to cozmo robot.

    """
    # vec.camera.image_stream_enabled = True
    vec_ros = VectorRos(vec)
    vec_ros.run()


if __name__ == '__main__':
    rospy.init_node('vector_driver')
    anki_vector.util.setup_basic_logging()
    try:
        with anki_vector.Robot(enable_camera_feed=True) as robot:
            vector_app(robot)
    except anki_vector.exceptions.VectorConnectionException as e:
        sys.exit('A connection error occurred: {}'.format(e))
