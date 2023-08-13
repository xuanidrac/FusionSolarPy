"""Client library to the fusion solar API"""

import json
import logging
import time
from datetime import datetime
from functools import wraps
from typing import Any, Optional

import bs4
import requests

from .exceptions import AuthenticationException, CaptchaRequiredException, FusionSolarException

# global logger object
_LOGGER = logging.getLogger(__name__)


class PowerStatus:
    """Class representing the basic power status"""

    def __init__(
        self,
        current_power_kw: float,
        total_power_today_kwh: float,
        total_power_kwh: float,
    ):
        """Create a new PowerStatus object
        :param current_power_kw: The currently produced power in kW
        :type current_power_kw: float
        :param total_power_today_kwh: The total power produced that day in kWh
        :type total_power_today_kwh: float
        :param total_power_kwh: The total power ever produced
        :type total_power_kwh: float
        """
        self.current_power_kw = current_power_kw
        self.total_power_today_kwh = total_power_today_kwh
        self.total_power_kwh = total_power_kwh

    def __repr__(self):
        return f"PowerStatus(current_power_kw={self.current_power_kw}, total_power_today_kwh={self.total_power_today_kwh}, total_power_kwh={self.total_power_kwh})"


def logged_in(func):
    """
    Decorator to make sure user is logged in.
    """

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            result = func(self, *args, **kwargs)
        except (json.JSONDecodeError, requests.exceptions.HTTPError):
            _LOGGER.info("Logging in")
            self._configure_session()
            result = func(self, *args, **kwargs)
        return result

    return wrapper


def with_solver(func):
    """
    Decorator to solve captchas when required
    """

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            result = func(self, *args, **kwargs)
        except (CaptchaRequiredException):
            _LOGGER.info("solving captcha and retrying login")
            # don't allow another captcha exception to be caught by this wrapper
            kwargs["allow_captcha_exception"] = False
            # check if captcha is required and populate self._verify_code
            # clear previous verify code if there was one for the check later
            self._verify_code = None
            captcha_present = self._check_captcha()
            if not captcha_present:
                raise AuthenticationException("Login failed: captcha required but not present.")

            if self._verify_code is not None:
                result = func(self, *args, **kwargs)
            else:
                raise AuthenticationException("Login failed: no verify code found.")
        return result

    return wrapper

class FusionSolarClient:
    """The main client to interact with the Fusion Solar API
    """

    def __init__(
        self, username: str, password: str, huawei_subdomain: str = "region01eu5",
        session: Optional[requests.Session] = None, model_path: Optional[str] = None, runtime: Optional[str] = "onnx", device: Optional[Any] = ['CPUExecutionProvider']
    ) -> None:
        """Initialiazes a new FusionSolarClient instance. This is the main
           class to interact with the FusionSolar API.
           The client tests the login credentials as soon as it is initialized
        :param username: The username for the system
        :type username: str
        :param password: The password
        :type password: str
        :param huawei_subdomain: The FusionSolar API uses different subdomains for different regions.
                                 Adapt this based on the first part of the URL when you access your system.
        :type huawei_subdomain: str
        :param session: An optional requests session object. If not set, a new session will be created.
        :type session: requests.Session
        :param model_path: Path to the weights file for the captcha solver. Only required if you want to use the auto captcha solver
        :type model_path: str
        :param runtime: The runtime for the captcha solver. Only required if you want to use the auto captcha solver. Supported runtimes: onnx, keras
        :type runtime: str
        :param device : The device to run the captcha solver on. Only required if you want to use the auto captcha solver
        """
        self._user = username
        self._password = password
        self._verify_code = None
        if session is None:
            self._session = requests.Session()
        else:
            self._session = session
        self._huawei_subdomain = huawei_subdomain
        # hierarchy: company <- plants <- devices <- subdevices
        self._company_id = None
        if self._huawei_subdomain.startswith("region"):
            self._login_subdomain = self._huawei_subdomain[8:]
        else:
            self._login_subdomain = self._huawei_subdomain

        self._model_path = model_path
        self._runtime = runtime
        self._device = device
        self._solver = None

        # Only login if no session has been provided. The session should hold the cookies for a logged in state
        if session is None:
            self._configure_session()

    def log_out(self):
        """Log out from the FusionSolarAPI
        """
        self._session.get(
            url=f"https://{self._huawei_subdomain}.fusionsolar.huawei.com/unisess/v1/logout",
            params={
                "service": f"https://{self._huawei_subdomain}.fusionsolar.huawei.com"
            },
        )

    def _check_captcha(self):
        """Checks if the captcha is required for the login.

        Also solves the captcha and places the answer into self._verify_code

        :returns True if captcha is required, False otherwise
        """
        _LOGGER.debug("Checking if captcha is required")

        url = f"https://{self._login_subdomain}.fusionsolar.huawei.com/"
        params = {
            "service": "%2Funisess%2Fv1%2Fauth%3Fservice%3D%252Fnetecowebext%252Fhome%252Findex.html",
        }
        r = self._session.get(url=url, params=params)
        r.raise_for_status()
        soup = bs4.BeautifulSoup(r.text, 'html.parser')
        captcha_exists = soup.find(id="verificationCodeInput")
        if captcha_exists:
            captcha = self._get_captcha()
            self._init_solver()
            self._verify_code = self._solver.solve_captcha(captcha)
            r = self._session.post(url=f"https://{self._login_subdomain}.fusionsolar.huawei.com/unisso/preValidVerifycode",
                                   data={"verifycode": self._verify_code, "index": 0})
            r.raise_for_status()
            if r.text != "success":
                raise AuthenticationException("Login failed: captcha prevalidverify fail.")
            return True
        else:
            return False

    def _get_captcha(self):
        url = f"https://{self._login_subdomain}.fusionsolar.huawei.com/unisso/verifycode"
        params = {"timestamp": round(time.time() * 1000)}
        r = self._session.get(url=url, params=params)
        r.raise_for_status()
        image_buffer = r.content
        return image_buffer

    def _init_solver(self):
        if self._model_path is None:
            return
        if self._solver is not None:
            return

        if self._runtime == "onnx":
            from .captcha_solver_onnx import Solver
            self._solver = Solver(self._model_path, self._device)
        elif self._runtime == "keras":
            from .captcha_solver_tf import Solver
            self._solver = Solver(self._model_path, self._device)

    @with_solver
    def _login(self, allow_captcha_exception=True):
        url = f"https://{self._login_subdomain}.fusionsolar.huawei.com/unisso/v2/validateUser.action"
        params = {
            "decision": 1,
            "service": f"https://{self._huawei_subdomain}.fusionsolar.huawei.com/unisess/v1/auth?service=/netecowebext/home/index.html#/LOGIN",
        }
        json_data = {
            "organizationName": "",
            "username": self._user,
            "password": self._password,
        }
        if self._verify_code:
            json_data["verifycode"] = self._verify_code
            # invalidate verify code after use
            self._verify_code = None

        # send the request
        r = self._session.post(url=url, params=params, json=json_data)
        r.raise_for_status()

        # make sure that the login worked
        error = None
        if r.json()["errorMsg"]:
            error = r.json()["errorMsg"]

        if error:
            if "incorrect verification code" in error.lower() and allow_captcha_exception:
                raise CaptchaRequiredException("Login failed: Incorrect verification code.")
            raise AuthenticationException(
                f"Failed to login into FusionSolarAPI: { r.json()['errorMsg'] }"
            )

    def _configure_session(self):
        """Logs into the Fusion Solar API. Raises an exception if the login fails.
        """
        # check the login credentials right away
        _LOGGER.debug("Logging into Huawei Fusion Solar API")

        self._login()

        # get the main id
        r = self._session.get(
            url=f"https://{self._huawei_subdomain}.fusionsolar.huawei.com/rest/neteco/web/organization/v2/company/current",
            params={"_": round(time.time() * 1000)},
        )
        r.raise_for_status()

        # catch an incorrect subdomain
        response_text = r.content.decode()

        if not response_text.strip().startswith("{\"data\":"):
            raise AuthenticationException("Invalid response received. Please check the correct Huawei subdomain.")

        response_data = r.json()

        if "data" not in response_data:
            _LOGGER.error(f"Failed to retrieve data object. {json.dumps(response_data)}")
            raise AuthenticationException(
                "Failed to login into FusionSolarAPI."
            )

        self._company_id = r.json()["data"]["moDn"]

        # get the roarand, which is needed for non-GET requests, thus to change device settings
        r = self._session.get(
            url=f"https://{self._huawei_subdomain}.fusionsolar.huawei.com/unisess/v1/auth/session"
        )
        r.raise_for_status()
        self._session.headers["roarand"] = r.json()[
            "csrfToken"
        ]  # needed for post requests, otherwise it will return 401

    @logged_in
    def get_power_status(self) -> PowerStatus:
        """Retrieve the current power status. This is the complete
           summary accross all stations.
        :return: The current status as a PowerStatus object
        """

        url = f"https://{self._huawei_subdomain}.fusionsolar.huawei.com/rest/pvms/web/station/v1/station/total-real-kpi"
        params = {
            "queryTime": round(time.time() * 1000),
            "timeZone": 1,
            "_": round(time.time() * 1000),
        }

        r = self._session.get(url=url, params=params)
        r.raise_for_status()
        power_obj = r.json()

        power_status = PowerStatus(
            current_power_kw=power_obj["data"]["currentPower"],
            total_power_today_kwh=power_obj["data"]["dailyEnergy"],
            total_power_kwh=power_obj["data"]["cumulativeEnergy"],
        )

        return power_status

    @logged_in
    def get_plant_ids(self) -> list:
        """Get the ids of all available stations linked
           to this account
        :return: A list of plant ids (strings)
        :rtype: list
        """
        # get the complete object tree
        station_list = self.get_station_list()

        # get the ids
        plant_ids = [obj["dn"] for obj in station_list]

        return plant_ids

    @logged_in
    def get_station_list(self) -> list:
        """Get the list of available PV stations.

        :return: _description_
        :rtype: list
        """
        # get the complete list
        r = self._session.post(
            url=f"https://{self._huawei_subdomain}.fusionsolar.huawei.com/rest/pvms/web/station/v1/station/station-list",
            json={
                "curPage": 1,
                "pageSize": 10,
                "gridConnectedTime": "",
                "queryTime": self._get_day_start_sec(),
                "timeZone": 2,
                "sortId": "createTime",
                "sortDir": "DESC",
                "locale": "en_US"
            }
        )
        r.raise_for_status()

        obj_tree = r.json()

        if not obj_tree["success"]:
            raise FusionSolarException("Failed to retrieve station list")

        # simply return the original object list
        return obj_tree["data"]["list"]

    @logged_in
    def get_device_ids(self) -> dict:
        """gets the devices associated to a given parent_id (can be a plant or a company/account)
        returns a dictionary mapping device_type to device_id"""
        url = f"https://{self._huawei_subdomain}.fusionsolar.huawei.com/rest/neteco/web/config/device/v1/device-list"
        params = {
            "conditionParams.parentDn": self._company_id,  # can be a plant or company id
            "conditionParams.mocTypes": "20814,20815,20816,20819,20822,50017,60066,60014,60015,23037",  # specifies the types of devices
            "_": round(time.time() * 1000),
        }
        r = self._session.get(url=url, params=params)
        r.raise_for_status()
        device_data = r.json()

        device_key = {}
        for device in device_data["data"]:
            device_key[device["mocTypeName"]] = device["dn"]
        return device_key

    @logged_in
    def active_power_control(self, power_setting) -> None:
        """apply active power control.
        This can be usefull when electrity prices are
        negative (sunny summer holiday) and you want
        to limit the power that is exported into the grid"""
        power_setting_options = {
            "No limit": 0,
            "Zero Export Limitation": 5,
            "Limited Power Grid (kW)": 6,
            "Limited Power Grid (%)": 7,
        }
        if power_setting not in power_setting_options:
            raise ValueError("Unknown power setting")

        device_key = self.get_device_ids()

        url = f"https://{self._huawei_subdomain}.fusionsolar.huawei.com/rest/pvms/web/device/v1/deviceExt/set-config-signals"
        data = {
            "dn": device_key["Dongle"],  # power control needs to be done in the dongle
            # 230190032 stands for "Active Power Control"
            "changeValues": f'[{{"id":"230190032","value":"{power_setting_options[power_setting]}"}}]',
        }

        r = self._session.post(url, data=data)
        r.raise_for_status()

    @logged_in
    def get_plant_flow(self, plant_id: str) -> dict:
        """Retrieves the data for the energy flow
        diagram displayed for each plant
        :param plant_id: The plant's id
        :type plant_id: str
        :return: The complete data structure as a dict
        """
        # https://region01eu5.fusionsolar.huawei.com/rest/pvms/web/station/v1/overview/energy-flow?stationDn=NE%3D33594051&_=1652469979488
        r = self._session.get(
            url=f"https://{self._huawei_subdomain}.fusionsolar.huawei.com/rest/pvms/web/station/v1/overview/energy-flow",
            params={"stationDn": plant_id, "_": round(time.time() * 1000)},
        )

        r.raise_for_status()
        flow_data = r.json()

        if not flow_data["success"] or "data" not in flow_data:
            raise FusionSolarException(f"Failed to retrieve plant flow for {plant_id}")

        return flow_data

    @logged_in
    def get_plant_stats(
        self, plant_id: str, query_time: int = None
    ) -> dict:
        """Retrieves the complete plant usage statistics for the current day.
        :param plant_id: The plant's id
        :type plant_id: str
        :param query_time: If set, must be set to 00:00:00 of the day the data should
                           be fetched for. If not set, retrieves the data for the
                           current day.
        :type query_time: int
        :return: _description_
        """
        # set the query time to today
        if not query_time:
            query_time = self._get_day_start_sec()

        r = self._session.get(
            url=f"https://{self._huawei_subdomain}.fusionsolar.huawei.com/rest/pvms/web/station/v1/overview/energy-balance",
            params={
                "stationDn": plant_id,
                "timeDim": 2,
                "queryTime": query_time,
                "timeZone": 2,  # 1 in no daylight
                "timeZoneStr": "Europe/Vienna",
                "_": round(time.time() * 1000),
            },
        )
        r.raise_for_status()
        plant_data = r.json()

        if not plant_data["success"] or "data" not in plant_data:
            raise FusionSolarException(
                f"Failed to retrieve plant status for {plant_id}"
            )

        # return the plant data
        return plant_data["data"]

    def get_last_plant_data(self, plant_data: dict) -> dict:
        """Extracts the last measurements from the plant data
        The dict contains detailed information about the data of the plant.
        If "existInverter" the "productPower" is reported.
        :param plant_data: The plant's stats data returned by get_plant_stats
        """
        # make sure the object is valid
        if "xAxis" not in plant_data:
            raise FusionSolarException("Invalid plant_data object passed.")

        measurement_times = plant_data["xAxis"]

        # initialize the extracted data
        extracted_data = {}

        # process the complete data
        for key_name in plant_data.keys():
            try:
                # fields to ignore
                if key_name in ("xAxis", "stationTimezone", "clientTimezone", "stationDn"):
                    continue

                key_value = plant_data[key_name]

                if type(key_value) is list:
                    extracted_data[key_name] = self._get_last_value(key_value, measurement_times)

                # Missing data
                elif key_value == "--":
                    extracted_data[key_name] = None

                # Boolean
                elif key_name.startswith("exist"):
                    extracted_data[key_name] = bool(key_value)

                # should be numeric
                else:
                    extracted_data[key_name] = float(key_value)
            # if anything goes wrong, simply store "None" as value
            except Exception as e:
                _LOGGER.debug(f"Failed to parse {key_name} = {key_value}")
                extracted_data[key_name] = None

        return extracted_data

    def _get_last_value(self, values: list, measurement_times: list):
        """Get the last valid value from a values array where
           missing values are stored as '--'
        :param values: The list of values
        :type values: list
        :param measurement_times: The list of matching timepoints
        :type values: list
        :return: A dict with a "value" and "timepoint"
        """
        # add all found values in a list
        found_values = list()

        for index, value in enumerate(values):
            if value != "--":
                found_values.append({"time": measurement_times[index], "value": float(values[index])})

        # if it's the last value
        if len(found_values) > 0:
            return found_values[-1]
        else:
            # If nothing is found return "None" for the current time
            return {"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "value": None}

    def _get_day_start_sec(self) -> int:
        """Return the start of the current day in seconds since
           epoche.

        :return: The start of the day ("00:00:00") in seconds
        :rtype: int
        """
        start_today = time.strftime("%Y-%m-%d 00:00:00", time.gmtime())
        struct_time = time.strptime(start_today, "%Y-%m-%d %H:%M:%S")
        seconds = round(time.mktime(struct_time) * 1000)

        return seconds

    @logged_in
    def get_optimizer_stats(
        self, inverter_id: str
    ) -> dict:
        """Retrieves the complete list of optimizers and returns real time stats.

        :param inverter_id: The inverter ID
        :type plant_id: str
        :return: _description_
        """
        r = self._session.get(
            url=f"https://{self._huawei_subdomain}.fusionsolar.huawei.com/rest/pvms/web/station/v1/layout/optimizer-info",
            params={
                "inverterDn": inverter_id,
                "_": round(time.time() * 1000),
            },
        )
        r.raise_for_status()
        optimizer_data = r.json()

        if not optimizer_data["success"] or "data" not in optimizer_data:
            raise FusionSolarException(
                f"Failed to retrieve plant status for {inverter_id}"
            )

        # return the plant data
        return optimizer_data["data"]
