"""
Driver for a Thorlabs K10CR2 rotation stage using the Kinesis ISC DLL.
"""

from ctypes import byref, c_char_p, c_double, c_int
import logging
import time

from thorlabs_kinesis import integrated_stepper_motors as ism

logger = logging.getLogger(__name__)


class K10CR2:
    """NanoNMR driver wrapper for a Thorlabs K10CR2 rotation stage."""

    DISTANCE_UNIT = 0
    MOVING_STATUS_BITS = 0x00000010 | 0x00000020 | 0x00000040 | 0x00000080
    HOMING_STATUS_BIT = 0x00000200
    HOMED_STATUS_BIT = 0x00000400

    def __init__(self, serial_id, poll_interval_ms=100):
        self.stage_serial_id = str(serial_id)
        self.serial_no = c_char_p(bytes(self.stage_serial_id, "utf-8"))
        self.poll_interval_ms = poll_interval_ms
        self.current_position = None
        self._is_open = False

        self._check_error("TLI_BuildDeviceList", ism.TLI_BuildDeviceList())
        logger.debug(f"Found {ism.TLI_GetDeviceListSize()} Thorlabs devices.")

        self._check_error("ISC_Open", ism.ISC_Open(self.serial_no))
        self._is_open = True

        try:
            self.hw_info = self.get_hardware_info()
            self._check_true("ISC_LoadSettings", ism.ISC_LoadSettings(self.serial_no))
            self._check_true(
                "ISC_StartPolling",
                ism.ISC_StartPolling(self.serial_no, self.poll_interval_ms),
            )
            time.sleep(1)

            self.enable()
            time.sleep(1)

            self.set_zero_backlash()
            self.update_positions_callback()

            logger.debug(
                f"Initialized K10CR2 stage with serial no. {int(self.stage_serial_id)}."
            )
        except Exception:
            self.close()
            raise

    @staticmethod
    def _check_error(label, error_code):
        if error_code != 0:
            raise RuntimeError(f"{label} failed with error code {error_code}")

    @staticmethod
    def _check_true(label, ok):
        if not ok:
            raise RuntimeError(f"{label} failed")

    def get_hardware_info(self):
        hw_info = ism.TLI_HardwareInformation()
        self._check_error(
            "ISC_GetHardwareInfoBlock",
            ism.ISC_GetHardwareInfoBlock(self.serial_no, byref(hw_info)),
        )
        return hw_info

    def deg_to_device_units(self, angle_deg):
        device_units = c_int()
        self._check_error(
            "ISC_GetDeviceUnitFromRealValue",
            ism.ISC_GetDeviceUnitFromRealValue(
                self.serial_no,
                c_double(float(angle_deg)),
                byref(device_units),
                self.DISTANCE_UNIT,
            ),
        )
        return device_units.value

    def device_units_to_deg(self, device_units):
        angle = c_double()
        self._check_error(
            "ISC_GetRealValueFromDeviceUnit",
            ism.ISC_GetRealValueFromDeviceUnit(
                self.serial_no,
                int(device_units),
                byref(angle),
                self.DISTANCE_UNIT,
            ),
        )
        return angle.value

    def update_positions_callback(self):
        self.current_position = self.get_position()
        logger.info(
            f"K10CR2 stage {int(self.stage_serial_id)} updated position = "
            f"{self.current_position} deg."
        )
        return self.current_position

    def get_position(self):
        self._check_error("ISC_RequestPosition", ism.ISC_RequestPosition(self.serial_no))
        time.sleep(0.1)
        return self.device_units_to_deg(ism.ISC_GetPosition(self.serial_no))

    def get_position_device_units(self):
        self._check_error("ISC_RequestPosition", ism.ISC_RequestPosition(self.serial_no))
        time.sleep(0.1)
        return ism.ISC_GetPosition(self.serial_no)

    def needs_homing(self):
        return ism.ISC_NeedsHoming(self.serial_no)

    def can_move_without_homing(self):
        return ism.ISC_CanMoveWithoutHomingFirst(self.serial_no)

    def get_status_bits(self):
        self._check_error(
            "ISC_RequestStatusBits",
            ism.ISC_RequestStatusBits(self.serial_no),
        )
        time.sleep(0.1)
        return ism.ISC_GetStatusBits(self.serial_no)

    def is_moving(self):
        return bool(self.get_status_bits() & self.MOVING_STATUS_BITS)

    def request_backlash(self):
        self._check_error("ISC_RequestBacklash", ism.ISC_RequestBacklash(self.serial_no))
        time.sleep(0.2)
        return ism.ISC_GetBacklash(self.serial_no)

    def set_zero_backlash(self):
        before = self.request_backlash()
        self._check_error("ISC_SetBacklash", ism.ISC_SetBacklash(self.serial_no, 0))
        after = self.request_backlash()
        logger.info(
            f"K10CR2 stage {int(self.stage_serial_id)} backlash set from "
            f"{before} to {after} device units."
        )
        return after

    def enable(self):
        self._check_error("ISC_EnableChannel", ism.ISC_EnableChannel(self.serial_no))
        return 0

    def disable(self):
        self._check_error("ISC_DisableChannel", ism.ISC_DisableChannel(self.serial_no))
        return 0

    def home(self, wait=True, timeout_s=60):
        logger.info(f"Homing K10CR2 stage {int(self.stage_serial_id)}...")
        self._check_error("ISC_Home", ism.ISC_Home(self.serial_no))
        if wait:
            self._wait_for_home(timeout_s=timeout_s)
            self.update_positions_callback()
        return 0

    def move_to_position(self, angle_deg, wait=True, timeout_s=30):
        if not self.can_move_without_homing():
            raise RuntimeError(
                "K10CR2 reports that it must be homed before motion."
            )

        target = self.deg_to_device_units(angle_deg)
        logger.info(
            f"Moving K10CR2 stage {int(self.stage_serial_id)} to {angle_deg} deg."
        )
        self._check_error(
            "ISC_MoveToPosition",
            ism.ISC_MoveToPosition(self.serial_no, target),
        )

        if wait:
            self._wait_for_position(target, timeout_s=timeout_s)

        self.update_positions_callback()
        return self.current_position

    def jog_position(self, angle_delta_deg, wait=True, timeout_s=30):
        if not self.can_move_without_homing():
            raise RuntimeError(
                "K10CR2 reports that it must be homed before motion."
            )

        start = self.get_position_device_units()
        delta = self.deg_to_device_units(angle_delta_deg)
        target = start + delta
        logger.info(
            f"Jogging K10CR2 stage {int(self.stage_serial_id)} by "
            f"{angle_delta_deg} deg."
        )
        self._check_error(
            "ISC_MoveRelative",
            ism.ISC_MoveRelative(self.serial_no, delta),
        )

        if wait:
            self._wait_for_position(target, timeout_s=timeout_s)

        self.update_positions_callback()
        return self.current_position

    def stop_motion(self):
        self._check_error("ISC_StopImmediate", ism.ISC_StopImmediate(self.serial_no))
        return 0

    def _wait_for_position(self, target_device_units, timeout_s=30, tolerance=1):
        start = time.monotonic()
        while time.monotonic() - start < timeout_s:
            position = self.get_position_device_units()
            if abs(position - target_device_units) <= tolerance:
                return position
            time.sleep(0.2)

        raise TimeoutError(
            f"Timed out waiting for K10CR2 stage {int(self.stage_serial_id)} "
            f"to reach {target_device_units} device units."
        )

    def _wait_for_home(self, timeout_s=60):
        start = time.monotonic()
        while time.monotonic() - start < timeout_s:
            status_bits = self.get_status_bits()
            homing = bool(status_bits & self.HOMING_STATUS_BIT)
            homed = bool(status_bits & self.HOMED_STATUS_BIT)
            if homed and not homing:
                return status_bits
            time.sleep(0.2)

        raise TimeoutError(
            f"Timed out waiting for K10CR2 stage {int(self.stage_serial_id)} "
            "to finish homing."
        )

    def close(self):
        if self._is_open:
            ism.ISC_StopPolling(self.serial_no)
            ism.ISC_Close(self.serial_no)
            self._is_open = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s.%(msecs)03d [%(levelname)8s] %(message)s",
        datefmt="%m-%d-%Y %H:%M:%S",
    )

    with K10CR2("55543994") as stage:
        print(f"Needs homing: {stage.needs_homing()}")
        print(f"Current position: {stage.current_position} deg")
        # stage.move_to_position(300.0)
        stage.home()
