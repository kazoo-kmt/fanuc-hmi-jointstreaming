from enum import Enum
import ftplib
from io import BytesIO
import threading
import time

import numpy as np

import actionlib
from control_msgs.msg import \
    FollowJointTrajectoryAction, \
    FollowJointTrajectoryResult
from miso_fanuc.status import FanucStatusMonitor
from miso_msgs.srv import SetJointSetpoint
import rospy


class ActionServer(object):
    """Hosts the actionlib interface for the FANUC robot.
    """

    __result = FollowJointTrajectoryResult()

    def __init__(self,
                 traj_topic,
                 server_address,
                 sim,
                 buffer_size=5):
        assert isinstance(traj_topic, str)
        assert isinstance(server_address, str)
        assert isinstance(sim, bool)
        assert isinstance(buffer_size, int)
        assert buffer_size > 0
        assert buffer_size < 10

        self.__robot_comm_lock = threading.Lock()
        self.__sim = sim
        self.__traj_topic = traj_topic
        self.__status_monitor = FanucStatusMonitor(server_address)

        self.__server_address = server_address

        self.__trajectory_asrv = actionlib.SimpleActionServer(
            self.__traj_topic,
            FollowJointTrajectoryAction,
            execute_cb=self.__execute_traj_callback,
            auto_start=False)
        self.__trajectory_asrv.start()

    def __execute_traj_callback(self, goal):
        if rospy.is_shutdown():
            return
        self.__robot_comm_lock.acquire()
        try:
            traj_runner = TrajRunner(self.__status_monitor)

            traj_runner.execute_trajectory(goal)
            self.__trajectory_asrv.set_succeeded()
        finally:
            self.__robot_comm_lock.release()


class TrajRunner(object):
    """Executes a trajectory by interpolating along a path,
    and slowing down execution when warning zones are observed
    """
    def __init__(self, status_monitor):
        self.__status_monitor = status_monitor
        self.exec_rate = 1. if status_monitor.zone == 0 else 0.
        svc = '/send_setpoint'
        rospy.wait_for_service(svc)
        self.send_setpoint = rospy.ServiceProxy(svc, SetJointSetpoint)

    def execute_trajectory(self, goal):
        rospy.logerr('Executing trajectory: ' + str(goal))
        param_time = 0.
        points = goal.trajectory.points
        def get_setpoint():
            start_point = points[0]
            end_point = points[-1]
            for i in range(len(points)):
                if points[i].time_from_start.to_sec() < param_time:
                    start_point = points[i]
                else:
                    end_point = points[i]
            dt = end_point.time_from_start.to_sec() - start_point.time_from_start.to_sec()
            if dt == 0.:
                prop_end = 1.
            else:
                prop_end = (param_time - start_point.time_from_start.to_sec()) / dt
            return np.array(start_point.positions)*(1-prop_end) + np.array(end_point.positions)*prop_end
        loop_start_time = time.time()
        while param_time < points[-1].time_from_start.to_sec():
            setpoint = get_setpoint()
            self.send_setpoint(joints=np.array(setpoint))
            loop_delta_time = time.time() - loop_start_time
            loop_start_time += loop_delta_time
            assert loop_delta_time < 0.2, 'Control loop not fast enough'
            param_time += self.exec_rate*loop_delta_time
            self.__update_exec_rate()
        self.send_setpoint(joints=np.array(points[-1].positions))

    def __update_exec_rate(self):
        if self.__status_monitor.zone == 0:
            self.exec_rate += 0.1
        elif self.__status_monitor.zone > 0:
            self.exec_rate -= 0.1
        self.exec_rate = min(max(self.exec_rate, 0.),1.)
