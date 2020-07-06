import h5py
import numpy as np
import scipy as sp
from scipy.integrate import simps
import unicodedata
import re
import os
import time
import tempfile
import inspect
from collections import deque
import warnings

import central_control.virt as virt
from central_control.k2400 import k2400
from central_control.pcb import pcb
from central_control.mppt import mppt
from central_control.illumination import illumination
from central_control.motion import motion
from central_control.put_ftp import put_ftp
import central_control  # for __version__

import sr830
import sp2150
import dp800
import virtual_sr830
import virtual_sp2150
import virtual_dp800
import eqe


class fabric:
    """ this class contains the sourcemeter and pcb control logic """

    outputFormatRevision = (
        "1.8.2"  # tells reader what format to expect for the output file
    )
    ssVocDwell = 10  # [s] dwell time for steady state voc determination
    ssIscDwell = 10  # [s] dwell time for steady state isc determination

    # start/end sweeps this many percentage points beyond Voc
    # bigger numbers here give better fitting for series resistance
    # at an incresed danger of pushing too much current through the device
    percent_beyond_voc = 50

    # guess at what the current limit should be set to (in amps) if we have no other way to determine it
    compliance_guess = 0.04

    # this is the datatype for the measurement in the h5py file
    measurement_datatype = np.dtype(
        {
            "names": ["voltage", "current", "time", "status"],
            "formats": ["f", "f", "f", "u4"],
            "titles": ["Voltage [V]", "Current [A]", "Time [s]", "Status bitmask"],
        }
    )

    # lockin measurement datatype in h5py file
    eqe_datatype = np.dtype(
        {
            "names": [
                "time",
                "wavelength",
                "x",
                "y",
                "r",
                "phase",
                "aux_in_1",
                "aux_in_2",
                "aux_in_3",
                "aux_in_4",
                "ref_frequency",
                "ch1_display",
                "ch2_display",
                "eqe",
                "integrated_jsc",
            ],
            "formats": [
                "f",
                "f",
                "f",
                "f",
                "f",
                "f",
                "f",
                "f",
                "f",
                "f",
                "f",
                "f",
                "f",
                "f",
                "f",
            ],
            "titles": [
                "Time [s]",
                "Wavelength [nm]",
                "X [V]",
                "Y [V]",
                "R [V]",
                "Phase [deg]",
                "Aux In 1 [V]",
                "Aux In 2 [V]",
                "Aux In 3 [V]",
                "Aux In 4 [V]",
                "Ref Frequency [Hz]",
                "CH1 display",
                "CH2 display",
                "EQE",
                "Integrated Jsc [ma/cm^2]",
            ],
        }
    )

    # this is the datatype for the status messages in the h5py file
    status_datatype = np.dtype(
        {
            "names": ["index", "message"],
            "formats": ["u4", h5py.special_dtype(vlen=str)],
            "titles": ["Index", "Message"],
        }
    )

    # this is an internal datatype to store the region of interest info
    roi_datatype = np.dtype(
        {
            "names": ["start_index", "end_index", "description"],
            "formats": ["u4", "u4", object],
            "titles": ["Start Index", "End Index", "Description"],
        }
    )

    spectrum_datatype = np.dtype(
        {
            "names": ["wavelength", "irradiance"],
            "formats": ["f", "f"],
            "titles": ["Wavelength [nm]", "Spectral Irradiance [W/m^2/nm]"],
        }
    )

    m = np.array(
        [], dtype=measurement_datatype
    )  # measurement list: columns = v, i, timestamp, status
    s = np.array(
        [], dtype=status_datatype
    )  # status list: columns = corresponding measurement index, status message
    r = np.array(
        [], dtype=roi_datatype
    )  # list defining regions of interest in the measurement list

    # init eqe data attribute with empty array
    eqe_data = np.array([], dtype=eqe_datatype)

    # function to use when sending ROIs to the GUI
    update_gui = None

    def __init__(self, saveDir=None, archive_address=None):
        self.saveDir = saveDir
        self.archive_address = archive_address

        self.software_revision = central_control.__version__
        print("Software revision: {:s}".format(self.software_revision))

    def __setattr__(self, attr, value):
        """here we can override what happends when we set an attribute"""
        if attr == "Voc":
            self.__dict__[attr] = value
            if value != None:
                print("V_oc is {:.4f}mV".format(value * 1000))

        elif attr == "Isc":
            self.__dict__[attr] = value
            if value != None:
                print("I_sc is {:.4f}mA".format(value * 1000))

        else:
            self.__dict__[attr] = value

    def connect(
        self,
        dummy=False,
        visa_lib="@py",
        smu_address=None,
        smu_terminator="\n",
        smu_baud=57600,
        mux_address=None,
        stage_address=None,
        light_address=None,
        lia_address=None,
        lia_terminator="\r",
        lia_baud=9600,
        lia_output_interface=0,
        mono_address=None,
        mono_terminator="\r",
        mono_baud=9600,
        psu_address=None,
        psu_terminator="\r",
        psu_baud=9600,
        ignore_adapter_resistors=True,
    ):
        """Connect to instruments.

        If any instrument addresses are `None`, virtual (fake) instruments are
        "connected" instead.

        Parameters
        ----------
        dummy : bool
            Choose whether or not to make all instruments virtual. Useful for testing
            control logic.
        visa_lib : str
            PyVISA backend.
        smu_address : str
            VISA resource name for the source-measure unit. If `None` is given a
            virtual instrument is created.
        smu_terminator : str
            Termination character for communication with the source-measure unit.
        smu_baud : int
            Baud rate for serial communication with the source-measure unit.
        mux_address : str
            VISA resource name for the multiplexor. If `None` is given a virtual
            instrument is created.
        stage_address : str
            VISA resource name for the translation stage. If `None` is given a virtual
            instrument is created.
        light_address : str
            VISA resource name for the light engine. If `None` is given a virtual
            instrument is created.
        lia_address : str
            VISA resource name for the lock-in amplifier. If `None` is given a virtual
            instrument is created.
        lia_terminator : str
            Termination character for communication with the lock-in amplifier.
        lia_baud : int
            Baud rate for serial communication with the lock-in amplifier.
        lia_output_interface : int
            Communication interface on the lock-in amplifier rear panel used to read
            instrument responses. This does not need to match the VISA resource
            interface type if, for example, an interface adapter is used between the
            control computer and the instrument. Valid output communication interfaces:
                * 0 : RS232
                * 1 : GPIB
        mono_address : str
            VISA resource name for the monochromator. If `None` is given a virtual
            instrument is created.
        mono_terminator : str
            Termination character for communication with the monochromator.
        mono_baud : int
            Baud rate for serial communication with the monochromator.
        psu_address : str
            VISA resource name for the LED power supply unit. If `None` is given a
            virtual instrument is created.
        psu_terminator : str
            Termination character for communication with the power supply unit.
        psu_baud : int
            Baud rate for serial communication with the power supply unit.
        ignore_adapter_resistors : bool
            Choose whether or not to measure the substrate pcb adapter resistors.
        """
        # source measure unit
        if (smu_address is None) or (dummy is True):
            self.sm = virt.k2400()
        else:
            self.sm = k2400(
                visa_lib=visa_lib,
                terminator=smu_terminator,
                addressString=smu_address,
                serialBaud=smu_baud,
            )
        self.sm_idn = self.sm.idn

        # instantiate max-power tracker object based on smu
        self.mppt = mppt(self.sm)

        # multiplexor
        if (mux_address is None) or (dummy is True):
            self.pcb = virt.pcb()
        else:
            self.pcb = pcb(
                address=mux_address, ignore_adapter_resistors=ignore_adapter_resistors
            )

        # light engine
        if (light_address is None) or (dummy is True):
            self.le = virt.illumination()
        else:
            self.le = illumination(address=light_address)
            self.le.connect()

        # translation stage
        if (stage_address is None) or (dummy is True):
            self.me = virt.motion()
        else:
            self.me = motion(address=stage_address)
            self.me.connect()

        # lock=in amplifier
        if (lia_address is None) or (dummy is True):
            self.lia = virtual_sr830.sr830(return_int=True)
        else:
            self.lia = sr830.sr830(return_int=True, check_errors=True)
            # default lia_output_interface is RS232
        self.lia.connect(
            resource_name=lia_address,
            output_interface=lia_output_interface,
            set_default_configuration=True,
        )
        self.lia_idn = self.lia.get_id()

        # monochromator
        if (mono_address is None) or (dummy is True):
            self.mono = virtual_sp2150.sp2150()
        else:
            self.mono = sp2150.sp2150()
        self.mono.connect(resource_name=mono_address)
        self.mono.set_scan_speed(1000)

        # bias LED PSU
        if (psu_address is None) or (dummy is True):
            self.psu = virtual_dp800.dp800()
        else:
            self.psu = dp800.dp800()
        self.psu.connect(resource_name=psu_address)
        self.psu_idn = self.psu.get_id()

    def hardwareTest(self, substrates_to_test):
        self.le.on()

        n_adc_channels = 8

        for chan in range(n_adc_channels):
            print(
                "ADC channel {:} Counts: {:}".format(chan, self.pcb.getADCCounts(chan))
            )

        chan = 2
        counts = self.pcb.getADCCounts(chan)
        print("{:d}\t<-- D1 Diode ADC counts (TP3, AIN{:d})".format(counts, chan))

        chan = 3
        counts = self.pcb.getADCCounts(chan)
        print("{:d}\t<-- D2 Diode ADC counts (TP4, AIN{:d})".format(counts, chan))

        chan = 0
        for substrate in substrates_to_test:
            r = self.pcb.get("d" + substrate)
            print(
                "{:s}\t<-- Substrate {:s} adapter resistor value in ohms (AIN{:d})".format(
                    r, substrate, chan
                )
            )

        print("LED test mode active on substrate(s) {:s}".format(substrates_to_test))
        print(
            "Every pixel should get an LED pulse IV sweep now, plus the light should turn on"
        )
        for substrate in substrates_to_test:
            sweepHigh = 0.01  # amps
            sweepLow = 0.0001  # amps

            ready_to_sweep = False

            # move to center of substrate
            self.me.goto(self.me.substrate_centers[ord(substrate) - ord("A")])

            for pix in range(8):
                pixel_addr = substrate + str(pix + 1)
                print(pixel_addr)
                if self.pcb.pix_picker(substrate, pix + 1):

                    if (
                        not ready_to_sweep
                    ):  # setup the sourcemeter if this is our first pixel
                        self.sm.setNPLC(0.01)
                        self.sm.setupSweep(
                            sourceVoltage=False,
                            compliance=2.5,
                            nPoints=101,
                            start=sweepLow,
                            end=sweepHigh,
                        )
                        self.sm.write(
                            ":arm:source bus"
                        )  # this allows for the trigger style we'll use here
                        ready_to_sweep = True

                    self.sm.updateSweepStart(sweepLow)
                    self.sm.updateSweepStop(sweepHigh)
                    self.sm.arm()
                    self.sm.trigger()
                    self.sm.opc()

                    self.sm.updateSweepStart(sweepHigh)
                    self.sm.updateSweepStop(sweepLow)
                    self.sm.arm()
                    self.sm.trigger()
                    self.sm.opc()

            self.sm.outOn(False)

            # deselect all pixels
            self.pcb.pix_picker(substrate, 0)

        self.le.off()

    def measureIntensity(
        self, diode_cal, ignore_diodes=False, recipe=None, spectrum_cal=None
    ):
        """Measure the equivalent solar intensity of the light source.

        Uses either reference calibration diodes on the sample stage and/or the light
        source's own internal calibration sensor.

        The function can return the number of suns and ADC counts for two stage-mounted
        diodes using diode calibration values in diode_cal (if diode_cal is not a tuple
        with valid calibration values, sets intensity to 1.0 sun for both diodes.)

        This function will try to calculate the equivalent solar intensity based on a
        measurement of the spectral irradiance if supported by the light source. To
        obtain the correct units and relative scaling for the spectral irradiance
        calibration data must be supplied as an argument to the function call. If
        calibration data is not supplied the raw measurement will be returned and the
        intensity is assumed to be 1.0 sun equivalent.

        Parameters
        ----------
        diode_cal : tuple
            ADC counts for 2 diodes corresponding to 1 sun equivalent illumination
            intensity.
        ignore_diodes : bool
            Choose whether or not to ignore measurement of stage mounted reference
            photodiodes, i.e. if they are not mounted, set this to True.
        recipe : str
            Name of the spectrum recipe for the light source to load.
        spectrum_cal : array-like
            Calibration data for the light source's internal spectrometer used to
            convert the raw measurement to units of spectral irradiance.
    """
        ret = {
            "diode_1_adc": None,
            "diode_2_adc": None,
            "diode_1_suns": None,
            "diode_2_suns": None,
            "wavelabs_suns": None,
        }

        self.spectrum = None

        if self.le.wavelabs is True:
            # if using wavelabs light engine, use internal spectrometer to measure
            # spectrum and intensity
            if recipe is not None:
                self.le.light_engine.activateRecipe(recipe)
            old_duration = self.le.light_engine.getRecipeParam(param="Duration")
            new_duration = 1
            self.le.light_engine.setRecipeParam(
                param="Duration", value=new_duration * 1000
            )
            run_ID = self.le.light_engine.on()
            self.le.light_engine.waitForRunFinished(run_ID=run_ID)
            self.le.light_engine.waitForResultAvailable(run_ID=run_ID)
            spectra = self.le.light_engine.getDataSeries(run_ID=run_ID)
            self.le.light_engine.setRecipeParam(param="Duration", value=old_duration)
            spectrum = spectra[0]
            wls = spectrum["data"]["Wavelenght"]
            irr = spectrum["data"]["Irradiance"]
            self.spectrum_raw = np.array(
                [[w, i] for w, i in zip(wls, irr)], dtype=self.spectrum_datatype
            )
            if spectrum_cal is None:
                self.spectrum = self.spectrum_raw
                ret["wavelabs_suns"] = 1
                warnings.warn(
                    "No spectral calibration supplied for Wavelabs simulator. Assuming 1.0 suns."
                )
            else:
                self.spectrum = self.spectrum_raw * spectrum_cal
                ret["wavelabs_suns"] = (
                    sp.integrare.simps(self.spectrum, wls) / 1000
                )  # intensity in suns

        if ignore_diodes is False:
            self.me.goto(self.me.photodiode_location)
            self.le.on()

            # if this is a real solar sim (not a virtual one), wait half a sec before
            # measuring intensity
            if type(self.le) == illumination:
                time.sleep(0.5)

            # measure diode counts
            ret["diode_1_adc"] = self.pcb.get("p1")
            ret["diode_2_adc"] = self.pcb.get("p2")

            self.le.off()

            if type(diode_cal) == list or type(diode_cal) == tuple:
                if diode_cal[0] <= 1:
                    warnings.warn(
                        "WARNING: No or bad intensity diode calibration values, assuming 1.0 suns"
                    )
                    ret["diode_1_suns"] = 1.0
                else:
                    ret["diode_1_suns"] = ret["diode_1_adc"] / diode_cal[0]

                if diode_cal[1] <= 1:
                    warnings.warn(
                        "WARNING: No or bad intensity diode calibration values, assuming 1.0 suns"
                    )
                    ret["diode_2_suns"] = 1.0
                else:
                    ret["diode_2_suns"] = ret["diode_2_adc"] / diode_cal[1]

        return ret

    def isWithinPercent(target, value, percent=10):
        """
    returns true if value is within percent percent of target, otherwise returns false
    """
        lb = target * (100 - percent) / 100
        ub = target * (100 + percent) / 100
        ret = False
        if lb <= value and value <= ub:
            ret = True
        return ret

    def runSetup(
        self,
        operator,
        diode_cal,
        ignore_diodes=False,
        run_description="",
        recipe=None,
        spectrum_cal=None,
    ):
        """
    stuff that needs to be done at the start of a run
    if light engine is wavelabs, it's internal spectrometer measures the spectrum and
    returns a single intensity value.
    otherwise, returns intensity tuple of length 4 where [0:1] are the raw ADC counts
    measured by the PCB's photodiodes and [2:3] are the number of suns of intensity.
    if diode_cal == True, suns intensity will be assumed and reported as 1.0.
    if type(diode_cal) == list, diode_cal[0] and [1] will be used to calculate number
    of suns.
    if ignore_diodes == True, diode ADC values will not be read and intensity =
    (1, 1, 1.0 1.0) will be used and reported.
    """
        self.run_dir = self.slugify(operator) + "-" + time.strftime("%y-%m-%d")

        if self.saveDir == None or self.saveDir == "__tmp__":
            td = tempfile.mkdtemp(suffix="_iv_data")
            # self.saveDir = td.name
            self.saveDir = td
            print("Using {:} as data storage location".format(self.saveDir))

        destinationDir = os.path.join(self.saveDir, self.run_dir)
        if not os.path.exists(destinationDir):
            os.makedirs(destinationDir)

        i = 0  # file name run integer
        save_file_prefix = "Run"
        # find the next unused run number
        files_here = os.listdir(destinationDir)
        while True:
            prefix = "{:}_{:}_".format(save_file_prefix, i)
            prefix_match = any([file.startswith(prefix) for file in files_here])
            if prefix_match:
                i += 1
            else:
                break
        save_file_full_path = os.path.join(
            destinationDir, "{:}{:}.h5".format(prefix, round(time.time()))
        )

        self.f = h5py.File(save_file_full_path, "x")
        print("Creating file {:}".format(self.f.filename))
        self.f.attrs["Operator"] = np.string_(operator)
        self.f.attrs["Timestamp"] = time.time()
        self.f.attrs["PCB Firmware Hash"] = np.string_(self.pcb.get("v"))
        self.f.attrs["Control Software Revision"] = np.string_(self.software_revision)
        self.f.attrs["Format Revision"] = np.string_(self.outputFormatRevision)
        self.f.attrs["Run Description"] = np.string_(run_description)
        self.f.attrs["Sourcemeter"] = np.string_(self.sm_idn)
        self.f.attrs["Lock-in amplifier"] = np.string_(self.lia_idn)
        self.f.attrs["Power supply"] = np.string_(self.psu_idn)

        intensity = self.measureIntensity(
            diode_cal, ignore_diodes, recipe, spectrum_cal
        )
        if self.le.wavelabs is True:
            self.f.attrs["Wavelabs intensity [suns]"] = intensity["wavelabs_suns"]

        if ignore_diodes is False:
            self.f.attrs["Diode 1 intensity [ADC counts]"] = np.int(
                intensity["diode_1_adc"]
            )
            self.f.attrs["Diode 2 intensity [ADC counts]"] = np.int(
                intensity["diode_2_adc"]
            )
            if type(diode_cal) == list:
                self.f.attrs["Diode 1 calibration [ADC counts]"] = np.int(diode_cal[0])
                self.f.attrs["Diode 2 calibration [ADC counts]"] = np.int(diode_cal[1])
            else:
                # we re-calibrated this run
                self.f.attrs["Diode 1 calibration [ADC counts]"] = np.int(
                    intensity["diode_1_adc"]
                )
                self.f.attrs["Diode 2 calibration [ADC counts]"] = np.int(
                    intensity["diode_2_adc"]
                )
            self.f.attrs["Diode 1 intensity [suns]"] = np.float(
                intensity["diode_1_suns"]
            )
            self.f.attrs["Diode 2 intensity [suns]"] = np.float(
                intensity["diode_2_suns"]
            )
            print(
                "Intensity = [{:0.4f} {:0.4f}] suns".format(
                    np.float(intensity[2]), np.float(intensity[3])
                )
            )

        return intensity

    def runDone(self):
        self.le.off()
        print("\nClosing {:s}".format(self.f.filename))
        this_filename = self.f.filename
        self.f.close()
        if self.archive_address is not None:
            if self.archive_address.startswith("ftp://"):
                with put_ftp(
                    self.archive_address + self.run_dir + "/", pasv=True
                ) as ftp:
                    with open(this_filename, "rb") as fp:
                        ftp.uploadFile(fp)

            else:
                print("WARNING: Could not understand archive url")

    def substrateSetup(self, position, suid="", layout_name=""):
        self.position = position
        if self.pcb.pix_picker(position, 0):
            self.f.create_group(position)

            self.f[position].attrs["Sample Unique Identifier"] = np.string_(suid)

            self.f[position].attrs[
                "Sample Adapter Board Resistor Value"
            ] = self.pcb.resistors[position]
            self.f[position].attrs["Sample Layout Name"] = np.string_(layout_name)

            return True
        else:
            return False

    def pixelSetup(self, pixel):
        """Call this to switch to a new pixel"""
        self.pixel = str(pixel[0][1])
        if self.pcb.pix_picker(pixel[0][0], pixel[0][1]):
            self.me.goto(pixel[2])  # move stage here
            self.area = pixel[1]

            self.f[self.position].create_group(self.pixel)
            self.f[self.position + "/" + self.pixel].attrs["area"] = (
                self.area * 1e-4
            )  # in m^2
            return True
        else:
            return False

    def pixelComplete(self):
        """Call this when all measurements for a pixel are complete"""
        self.pcb.pix_picker(self.position, 0)
        m = self.f[self.position + "/" + self.pixel].create_dataset(
            "all_measurements", data=self.m, compression="gzip"
        )
        for i in range(len(self.r)):
            m.attrs[self.r[i][2]] = m.regionref[self.r[i][0] : self.r[i][1]]
        self.f[self.position + "/" + self.pixel].create_dataset(
            "status_list", data=self.s, compression="gzip"
        )
        self.f[self.position + "/" + self.pixel].create_dataset(
            "eqe", data=self.eqe, compression="gzip"
        )
        self.m = np.array(
            [], dtype=self.measurement_datatype
        )  # reset measurement storage
        self.s = np.array([], dtype=self.status_datatype)  # reset status storage
        self.r = np.array([], dtype=self.roi_datatype)  # reset region of interest
        self.eqe_data = np.array([], dtype=self.eqe_datatype)  # reset eqe data
        self.Voc = None
        self.Isc = None
        self.mppt.reset()

    def slugify(self, value, allow_unicode=False):
        """
    Convert to ASCII if 'allow_unicode' is False. Convert spaces to hyphens.
    Remove characters that aren't alphanumerics, underscores, or hyphens.
    Convert to lowercase. Also strip leading and trailing whitespace.
    """
        value = str(value)
        if allow_unicode:
            value = unicodedata.normalize("NFKC", value)
        else:
            value = (
                unicodedata.normalize("NFKD", value)
                .encode("ascii", "ignore")
                .decode("ascii")
            )
        value = re.sub(r"[^\w\s-]", "", value).strip().lower()
        return re.sub(r"[-\s]+", "-", value)

    def insertStatus(self, message):
        """adds status message to the status message list"""
        print(message)
        s = np.array((len(self.m), message), dtype=self.status_datatype)
        self.s = np.append(self.s, s)

    def registerMeasurements(self, measurements, description):
        """adds an array of measurements to the master list and creates an ROI for them
    takes new measurement numpy array and description of them"""
        roi = {}
        roi["v"] = [float(e[0]) for e in measurements]
        roi["i"] = [float(e[1]) for e in measurements]
        roi["t"] = [float(e[2]) for e in measurements]
        roi["s"] = [float(e[3]) for e in measurements]
        roi["message"] = description
        roi["area"] = self.area
        try:
            self.update_gui(roi)  # send the new region of interest data to the GUI
        except:
            pass  # probably no gui server to send data to, NBD
        self.m = np.append(self.m, measurements)
        length = len(measurements)
        if length > 0:
            stop = len(self.m) - 1
            start = stop - length + 1
            print(
                "New region of interest: [{:},{:}]\t{:s}".format(
                    start, stop, description
                )
            )
            r = np.array((start, stop, description), dtype=self.roi_datatype)
            self.r = np.append(self.r, r)
        else:
            print("WARNING: Non-positive ROI length")

    def steadyState(
        self,
        t_dwell=10,
        NPLC=10,
        stepDelay=0.005,
        sourceVoltage=True,
        compliance=0.04,
        setPoint=0,
        senseRange="f",
        handler=None,
    ):
        """ makes steady state measurements for t_dwell seconds
    set NPLC to -1 to leave it unchanged
    returns array of measurements
    """
        self.insertStatus(
            "Measuring steady state {:s} at {:.0f} m{:s}".format(
                "current" if sourceVoltage else "voltage",
                setPoint * 1000,
                "V" if sourceVoltage else "A",
            )
        )
        if NPLC != -1:
            self.sm.setNPLC(NPLC)
        self.sm.setStepDelay(stepDelay)
        self.sm.setupDC(
            sourceVoltage=sourceVoltage,
            compliance=compliance,
            setPoint=setPoint,
            senseRange=senseRange,
        )
        self.sm.write(
            ":arm:source immediate"
        )  # this sets up the trigger/reading method we'll use below
        q = self.sm.measureUntil(t_dwell=t_dwell, handler=handler)
        qa = np.array([tuple(s) for s in q], dtype=self.measurement_datatype)
        return qa

    def sweep(
        self,
        sourceVoltage=True,
        senseRange="f",
        compliance=0.04,
        nPoints=1001,
        stepDelay=0.005,
        start=1,
        end=0,
        NPLC=1,
        message=None,
        handler=None,
    ):
        """ make a series of measurements while sweeping the sourcemeter along linearly progressing voltage or current setpoints
    """

        self.sm.setNPLC(NPLC)
        self.sm.setStepDelay(stepDelay)
        self.sm.setupSweep(
            sourceVoltage=sourceVoltage,
            compliance=compliance,
            nPoints=nPoints,
            start=start,
            end=end,
            senseRange=senseRange,
        )

        if message == None:
            word = "current" if sourceVoltage else "voltage"
            abv = "V" if sourceVoltage else "A"
            message = "Sweeping {:s} from {:.0f} m{:s} to {:.0f} m{:s}".format(
                word, start, abv, end, abv
            )
        self.insertStatus(message)
        raw = self.sm.measure()
        sweepValues = np.array(
            list(zip(*[iter(raw)] * 4)), dtype=self.measurement_datatype
        )
        if handler is not None:
            handler(raw)

        return sweepValues

    def track_max_power(
        self,
        duration=30,
        message=None,
        NPLC=-1,
        stepDelay=-1,
        extra="basic://7:10",
        handler=None,
    ):
        if message == None:
            message = "Tracking maximum power point for {:} seconds".format(duration)
        self.insertStatus(message)
        raw = self.mppt.launch_tracker(
            duration=duration, NPLC=NPLC, extra=extra, handler=handler
        )
        # raw = self.mppt.launch_tracker(duration=duration, callback=fabric.mpptCB, NPLC=NPLC)
        qa = np.array([tuple(s) for s in raw], dtype=self.measurement_datatype)
        self.registerMeasurements(qa, "MPPT")

        if self.mppt.Vmpp != None:
            self.f[self.position + "/" + self.pixel].attrs["Vmpp"] = self.mppt.Vmpp
        if self.mppt.Impp != None:
            self.f[self.position + "/" + self.pixel].attrs["Impp"] = self.mppt.Impp
        if (self.mppt.Impp != None) and (self.mppt.Vmpp != None):
            self.f[self.position + "/" + self.pixel].attrs["ssPmax"] = abs(
                self.mppt.Impp * self.mppt.Vmpp
            )

    def mpptCB(measurement):
        """Callback function for max power point tracker
    (for live tracking)
    """
        [v, i, t, status] = measurement
        print("At {:.6f}\t{:.6f}\t{:.6f}\t{:d}".format(t, v, i, int(status)))

    def eqe(
        self,
        psu_ch1_voltage=0,
        psu_ch1_current=0,
        psu_ch2_voltage=0,
        psu_ch2_current=0,
        psu_ch3_voltage=0,
        psu_ch3_current=0,
        smu_voltage=0,
        calibration=True,
        ref_measurement_path=None,
        ref_measurement_file_header=1,
        ref_eqe_path=None,
        ref_spectrum_path=None,
        start_wl=350,
        end_wl=1100,
        num_points=76,
        repeats=1,
        grating_change_wls=None,
        filter_change_wls=None,
        auto_gain=True,
        auto_gain_method="user",
        integration_time=8,
        handler=None,
    ):
        """Run EQE scan."""

        # open all mux relays if calibrating
        if calibration is True:
            self.pcb.write("s")

        eqe_data = eqe.scan(
            self.lia,
            self.mono,
            self.psu,
            self.sm,
            psu_ch1_voltage,
            psu_ch1_current,
            psu_ch2_voltage,
            psu_ch2_current,
            psu_ch3_voltage,
            psu_ch3_current,
            smu_voltage,
            calibration,
            ref_measurement_path,
            ref_measurement_file_header,
            ref_eqe_path,
            ref_spectrum_path,
            start_wl,
            end_wl,
            num_points,
            repeats,
            grating_change_wls,
            filter_change_wls,
            auto_gain,
            auto_gain_method,
            integration_time,
            handler,
        )

        eqe_data = np.array(eqe_data, dtype=self.eqe_datatype)
        self.eqe_data = eqe_data  # added to data file when pixelComplete is called
        if calibration is not True:
            # add integrated Jsc attribute to data file
            self.f[self.position + "/" + self.pixel].attrs["integrated_jsc"] = eqe_data[
                -1, -1
            ]

        return eqe_data

    def calibrate_psu(self, channel=1, loc=None, handler=None):
        """Calibrate the LED PSU.

        Measure the short-circuit current of a photodiode generated upon illumination
        with an LED powered by the PSU as function of PSU current.

        Parameters
        ----------
        channel : {1, 2, 3}
            PSU channel.
        loc : list
            Position of calibration photodiode.
        handler : DataHandler
            Handler to process data.
        """
        currents = np.linspace(0, 1, 11, endpoint=True)

        # move to photodiode
        if loc is not None:
            self.me.goto(loc)

        # open all mux relays
        self.pcb.write("s")

        # set smu to short circuit and enable output
        self.sm.setupDC(
            sourceVoltage=True, compliance=0.1, setPoint=0, senseRange="a",
        )
        self.sm.outOn(True)

        # set up PSU
        self.psu.set_apply(channel=channel, voltage="MAX", current=0)
        self.psu.set_output_enable(True, channel)

        for current in currents:
            self.psu.set_apply(channel=channel, voltage="MAX", current=current)
            time.sleep(1)
            data = self.sm.measure()
            data.append(current)
            if handler is not None:
                handler.handle_data(data)

        # disable PSU
        self.psu.set_apply(channel=channel, voltage="MAX", current=0)
        self.psu.set_output_enable(False, channel)

        # disable smu
        self.sm.outOn(False)

