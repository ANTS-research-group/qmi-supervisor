from logging.handlers import TimedRotatingFileHandler
import os, sys, signal, gi, time, threading, logging, argparse
from datetime import datetime
from gpsdclient import GPSDClient

import psutil

import config_data as config
import udhcpc
gi.require_version('Qmi', '1.0')
from gi.repository import GLib, Gio, Qmi
from modules.services.nas_client import NASClient
from modules.services.dms_client import DMSClient
from modules.services.wds_client import WDSClient
from modules.device.device import Device

main_loop = None

def signal_handler(data):
    main_loop.quit()

def open_ready(qmidev,result,user_data=None):

    device = Device.get_instance()
    try:
        qmidev.open_finish(result)

    except GLib.GError as error:
        config.log.error("Couldn't open QMI device: %s" % error.message)
        device.state = config.STATE_DEVICE_DISCONNECT_MODE
        main_loop.quit()
        
        return

    dms_client = DMSClient(qmidev, main_loop)
    nas_client = NASClient(qmidev, main_loop)
    wds_client = WDSClient(qmidev, main_loop)
    
    dms_client.get_dms_info()
    nas_client.get_nas_info()

    if config.GeneralConfig.exit_if_not_connect_to_network == True and device.retries_in_wds_info >= config.MAX_RETRIES_IN_WDS_INFO:
        device.retries_in_wds_info = 0
        device.state = config.STATE_DEVICE_DISCONNECT_MODE
        config.log.error("STATE_DEVICE_DISCONNECT_MODE was configurated")
    else:
        wds_client.get_wds_info()

def connect_ready(qmidev,result,user_data=None):

    device = Device.get_instance()
    try:
        qmidev.open_finish(result)

    except GLib.GError as error:
        config.log.error("Couldn't connect QMI device: %s" % error.message)
        device.state = config.STATE_DEVICE_RESET_MODE
        main_loop.quit()
        return
    
    wds_client = WDSClient(qmidev, main_loop)
    wds_client.wds_start_network()

def check_packet_service_status(qmidev,result,user_data=None):

    device = Device.get_instance()
    try:
        qmidev.open_finish(result)

    except GLib.GError as error:
        config.log.error("Couldn't open QMI device: %s" % error.message)
        device.state = config.STATE_DEVICE_RESET_MODE
        main_loop.quit()
        return
    
    wds_client = WDSClient(qmidev, main_loop)
    wds_client.wds_packet_service_status()

def disconnect_ready(qmidev,result,user_data=None):
    device = Device.get_instance()
    try:
        qmidev.open_finish(result)
    except GLib.GError as error:
        config.log.error("Couldn't open QMI device: %s" % error.message)
        device.state = config.STATE_DEVICE_RESET_MODE
        main_loop.quit()
        return
    
    wds_client = WDSClient(qmidev, main_loop)
    wds_client.wds_stop_network()

def new_ready(unused, result, user_data=None):
    device = Device.get_instance()
    try:
        qmidev = Qmi.Device.new_finish(result)
    except GLib.GError as error:
        config.log.error("Couldn't create QMI device: %s" % error.message)

        device.wwan_interface = None
        
        if (error.code == 1):
            device.state = config.STATE_DEVICE_RESET_MODE
            config.log.error("STATE_DEVICE_RESET_MODE was configurated")
        else: 
            device.state = config.STATE_DEVICE_DISCONNECT_MODE
            config.log.error("STATE_DEVICE_DISCONNECT_MODE was configurated")

        main_loop.quit()
        return
    
    #save wwan iface if not exists
    if (device.wwan_interface is None):
        device.wwan_interface = qmidev.get_wwan_iface()
        config.log.info("wwan interface was configured recently")
        main_loop.quit()
        return

    device.all_info["log_id"] += 1
    start_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
    device.set_start_datetime(start_datetime)

    # the device has a packet data handler when is connected to data network
    open_flags = int(Qmi.DeviceOpenFlags.PROXY) + int(Qmi.DeviceOpenFlags.NET_RAW_IP) + int(Qmi.DeviceOpenFlags.NET_NO_QOS_HEADER)
    open_flags_mask = Qmi.DeviceOpenFlags(open_flags)

    match device.state:
        case config.STATE_DEVICE_CONNECT_MODE:
            qmidev.open(open_flags_mask, 10, None, connect_ready, None)
        case config.STATE_DEVICE_CHECK_IS_CONNECTED:
            qmidev.open(open_flags_mask, 10, None, check_packet_service_status, None)
        case config.STATE_DEVICE_CHECK_IS_DISCONNECTED:
            qmidev.open(open_flags_mask, 10, None, check_packet_service_status, None)
        case config.STATE_DEVICE_GET_INFO_MODE:
            qmidev.open(open_flags_mask, 20, None, open_ready, None)
        case config.STATE_DEVICE_DISCONNECT_MODE:
            qmidev.open(open_flags_mask, 10, None, disconnect_ready, None)

def get_location():

    with GPSDClient() as client:
            
            gps_tpv = {}
            gps_sky = {}
            for result in client.dict_stream(filter=["TPV", "SKY"]):

                device = Device.get_instance()
                now = datetime.now()
                timestamp = str(now.timestamp())
                datetime_gps = now.strftime("%d/%m/%Y %H:%M:%S.%f")

                if result:

                    if result["class"] == "TPV":
                        
                        gps_tpv.update({"timestamp": timestamp})
                        gps_tpv.update({"datetime": datetime_gps})
                        gps_tpv.update({"lat": result.get("lat", None)})
                        gps_tpv.update({"lon": result.get("lon", None)})
                        gps_tpv.update({"alt": result.get("alt", None)})
                        gps_tpv.update({"althae": result.get("altHAE", None)})
                        gps_tpv.update({"epx": result.get("epx", None)})
                        gps_tpv.update({"epy": result.get("epy", None)})
                        gps_tpv.update({"epv": result.get("epv", None)})
                        gps_tpv.update({"speed": result.get("speed", None)})
                        gps_tpv.update({"eps": result.get("eps", None)})

                        device.loc_gps.update({"tpv": gps_tpv})
                        gps_sky = {}

                    elif result["class"] == "SKY":  
                        
                        gps_sky.update({"timestamp": timestamp})
                        gps_sky.update({"datetime": datetime_gps})
                        gps_sky.update({"hdop": result.get("hdop", None)})
                        gps_sky.update({"pdop": result.get("pdop", None)})
                        
                        device.loc_gps.update({"sky": gps_sky})

                        gps_tpv = {}
                        gps_sky = {}

                    else:
                        config.log.error(f"gps location wasn't got")

                else:  
                    config.log.error(f"result gps location wasn't found")

def get_throughput():

    UPDATE_DELAY = 0.5 # in seconds

    io = psutil.net_io_counters(pernic=True)

    while True:
        
        time.sleep(UPDATE_DELAY)

        device = Device.get_instance()

        if device.wwan_interface == None:
                continue
        
        # get the network I/O stats again per interface
        io_2 = psutil.net_io_counters(pernic=True)
        # initialize the data to gather (a list of dicts)

        for iface, iface_io in io.items():

            if iface != device.wwan_interface and iface in io_2:
                continue

            # new - old stats gets us the speed
            upload_speed, download_speed = io_2[iface].bytes_sent - iface_io.bytes_sent, io_2[iface].bytes_recv - iface_io.bytes_recv

            throughput = {
                
                "throughput_upload_kb": round(io_2[iface].bytes_sent / 1024 , 2),                       # in kb
                "throughput_download_kb": round(io_2[iface].bytes_recv / 1024, 2),                      # in kb
                "throughput_upload_speed_kbps": round((upload_speed / UPDATE_DELAY) / 1024 * 8, 2),     # in kbps
                "throughput_download_speed_kbps": round((download_speed / UPDATE_DELAY) / 1024 * 8, 2)  # in kbps

            }
            
            device.throughput = throughput

            throughput = {}

        io = io_2

if __name__ == "__main__":

    if os.geteuid() != 0:
        print('ERROR: you need root or sudo permissions to do this')
        sys.exit(-1)

    parser_args = argparse.ArgumentParser(description='QMI-Supervisor')
    parser_args.add_argument('--device', '-d', metavar='DEVICE_FILE', type=str, help='Especify QMI USB device (e.g /dev/cdc-wdm0)', required=True)

    args = parser_args.parse_args()
    
    if args.device:
        if not os.path.exists(args.device):
            print("ERROR: qmi device file doesn't exists")
            sys.exit(1)
        else:
            device_founded = False
            for info_device in config.info_device_list:
                if info_device != None and info_device.device_path == args.device:
                    device = Device.get_instance()
                    device.set_info_device(info_device)
                    device_founded = True
                    break
            if (device_founded == False):
                print("ERROR: qmi device file path wasn't configured in config_data.py (InfoDevice class object) correctly")
                sys.exit(1)
    else:
        sys.exit(1)
    

    log_file_path = f'/tmp/{os.path.basename(args.device)}_{config.LoggingConfig.log_file_path}'

    # first remove log file if exists
    if os.path.exists(log_file_path):
        os.remove(log_file_path)

    with open(log_file_path, 'w') as archivo:
        archivo.write("")

    log_file_handler_format = logging.Formatter('%(asctime)s - %(levelname)s - [line %(lineno)d in %(filename)s] %(message)s ')

    log_file_handler = logging.FileHandler(filename=log_file_path, encoding='utf-8')
    log_file_handler.setLevel(config.LoggingConfig.log_debug_level_file)
    log_file_handler.setFormatter(log_file_handler_format)
    log_file_handler.mode = 'w'

    consoleHandler = logging.StreamHandler(sys.stdout)
    consoleHandler.setLevel(config.LoggingConfig.log_debug_level_console)
    consoleHandler.setFormatter(log_file_handler_format)

    rotatingHandler = TimedRotatingFileHandler(filename=log_file_path, when="d", interval=1, backupCount=7, encoding='utf-8')
    rotatingHandler.setLevel(config.LoggingConfig.log_debug_level_file)
    rotatingHandler.setFormatter(log_file_handler_format)
    rotatingHandler.mode = 'w'

    log = logging.getLogger("clients")
    log.setLevel(config.LoggingConfig.log_max_debug_level)
    log.addHandler(consoleHandler)
    # log.addHandler(log_file_handler)
    log.addHandler(rotatingHandler)
    
    config.log = log
    
    config.log.info("starting qmi-supervisor process")    
    
    file = Gio.File.new_for_path(args.device)

    #gpsd process

    if (config.GeneralConfig.get_location):
        loc_thread = threading.Thread(target=get_location)
        loc_thread.daemon = True
        loc_thread.start()

    if (config.GeneralConfig.get_throughput):

        tp_thread = threading.Thread(target=get_throughput)
        tp_thread.daemon = True
        tp_thread.start()
    
    delay_seconds = int(config.GeneralConfig.delay_seconds)

    try:
        
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGHUP, signal_handler, None)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, signal_handler, None)

        while True:
            
            start_time = time.time()

            device = Device.get_instance()

            #start - check if apn was changed in config file
            if (device.apn != config.DataNetwork5GConfig.apn):
                device.apn = config.DataNetwork5GConfig.apn
                device.wwan_interface = None
                device.state = config.STATE_DEVICE_CONNECT_MODE
                config.log.info(f"configuring the device with a new apn {device.apn}")

            match device.state:
                case config.STATE_DEVICE_RESET_MODE:

                    config.log.error("STATE_DEVICE_RESET_MODE has been configured")

                    result_reset = False

                    while device.retries_to_reset >= 0 and not result_reset:

                        if not os.path.exists(args.device):
                            
                            config.log.error("No such file or directory")
                            config.log.error("sleeping for 20 seconds and exiting")
                            time.sleep(20)
                            sys.exit(1)
                            
                        result_reset = device.check_manual()

                        if not result_reset:
                            config.log.error("the device wasn't correctly configured ")
                            device.retries_to_reset = device.retries_to_reset - 1
                        
                        

                    if (device.retries_to_reset <= 0):
                        config.log.error("max retries limit to reset device was reached")
                        sys.exit(1)
                        
                    else:
                        device.retries_to_reset = config.MAX_RETRIES_TO_RESET_DEVICE
                        device.wwan_interface = None
                        device.state = config.STATE_DEVICE_CONNECT_MODE

                case config.STATE_DEVICE_DISCONNECTED_MODE:

                    config.log.error("disconnected state was configured because of an error") 

                    if (len(device.all_info['clients']['wds']['info']) == 0):

                        time.sleep(1)

                        config.log.error(f"there are not packets of data network - empty wds field")

                        device.retries_without_data = device.retries_without_data + 1
                        
                        if (device.retries_without_data >= config.MAX_RETRIES_WITHOUT_DATA):
                            device.retries_without_data = 0
                            # device.wwan_interface = None
                            device.state = config.STATE_DEVICE_RESET_MODE

                            config.log.error(f"max retries without data was reached so this device will be reseted")
                            
                        else:
                            config.log.error("trying again...")
                            # TODO: Verify this
                            # device.wwan_interface = None
                            device.state = config.STATE_DEVICE_CONNECT_MODE
                    

                    if (config.GeneralConfig.exit_if_not_connect_to_network):
                        config.log.error("device couldn't connect to network and exit_if_not_connect_to_network is enabled")
                        config.log.error("device will be reseted and then its execution will be finished.")
                        config.log.error("reseting...")
                        device.check_manual()
                        config.log.error("finishing...")
                        sys.exit(1)

                        

                    # if (device.retries_without_data >= config.MAX_RETRIES_WITHOUT_DATA):
                    #     device.retries_without_data = 0
                    #     device.wwan_interface = None
                    #     device.state = config.STATE_DEVICE_RESET_MODE

                    #     config.log.error(f"state switch to STATE_DEVICE_CONNECT_MODE")
                    #     continue
                    
                    # config.log.error("disconnected state was configured because of an error") 

                    # if (config.GeneralConfig.exit_if_not_connect_to_network):
                    #     config.log.error("device couldn't connect to network and exit_if_not_connect_to_network is enabled")
                    #     config.log.error("restarting device...")
                    #     device.check_manual()
                    #     sys.exit(1)
                    # else:
                    #     config.log.error("trying again...")
                    #     device.wwan_interface = None
                    #     device.state = config.STATE_DEVICE_CONNECT_MODE

                case config.STATE_DEVICE_CONNECT_MODE:
                    if (config.GeneralConfig.connect_to_network):
                        config.log.info("pre-config device running...")

                        if (device.wwan_interface is not None):
                            try:
                                udhcpc.pre_config(device.wwan_interface)
                                config.log.info("pre-config device was done successfully...")
                            except Exception as e:
                                config.log.error(e)
                                config.log.error("the device couldn't be configured")
                                device.state = config.STATE_DEVICE_RESET_MODE
                                config.log.error("reset mode was configured")
                                continue
                        else:
                            config.log.debug("wwan interface wasn't found")

                case config.STATE_DEVICE_GET_INFO_MODE:
                    
                    start_time_info = time.time()

                    # get gps location
                    if (config.GeneralConfig.get_location):
                        device.get_gpsd_location()

                    # get throughput
                    if (config.GeneralConfig.get_throughput):
                        device.get_throughput()

                    # TODO: First, check if device has IP Address 5G
                    try:
                        device.send_to_prometheus_collector()
                        
                    except:
                        device.state = config.STATE_DEVICE_DISCONNECTED_MODE
                    
                    # show, save and send all data
                    if (device.create_dir_struct()):
                    
                        device.save_to_csv()
                        device.save_to_json()
                        device.send_to_broadcast()
                        device.reset_info_structs()

                    end_time_info = time.time()
                    total_time = round(end_time_info - start_time_info, 4)
                    # config.log.info(f"time elapsed in GET_INFO_MODE {total_time}") 

                case config.STATE_DEVICE_CONNECTED_MODE:
                    #TODO: si necesita DHCP, cambiar a DHCP MODE, si no GET INFO MODE
                    if (config.GeneralConfig.launch_udhcpc):
                        device.state = config.STATE_DEVICE_DHCP_MODE
                    else:
                        device.state = config.STATE_DEVICE_GET_INFO_MODE
                    continue
                    
                case config.STATE_DEVICE_DHCP_MODE:

                    config.log.info("dhcp service running...")

                    if (device.wwan_interface is None):
                        config.log.error("wwan interface wasn't found")
                        device.state = config.STATE_DEVICE_DISCONNECTED_MODE
                        continue
                    else:
                        config.log.info(f"An interface named {device.wwan_interface} was found")

                    if (udhcpc.start(ifname=device.wwan_interface)):
                        config.log.info("the configuration parameters was successfully achieved.")
                        
                        if (udhcpc.update_metric(ifname=device.wwan_interface, metric=config.GeneralConfig.metric_dhcp)):
                            config.log.info("the metric of route table was successfully updated.")
                        else:
                            config.log.info("there was an error while metric of route was updating")
                        
                        device.state = config.STATE_DEVICE_GET_INFO_MODE
                    else:
                        
                        config.log.warning("couldn't connect to dhcp")
                        
                        if (device.pdh != None and device.pdh > 0):
                            device.state = config.STATE_DEVICE_DISCONNECT_MODE
                        else:
                            device.state = config.STATE_DEVICE_DISCONNECTED_MODE
                    continue

            # CASES: CONNECT_MODE, CHECK_PACKET_SERVICE_STATUS_MODE, GET_INFO_MODE, DISCONNECT_MODE
            if (device.state == config.STATE_DEVICE_CONNECT_MODE or device.state == config.STATE_DEVICE_CHECK_IS_CONNECTED or device.state == config.STATE_DEVICE_CHECK_IS_DISCONNECTED
                or device.state == config.STATE_DEVICE_GET_INFO_MODE or device.state == config.STATE_DEVICE_DISCONNECT_MODE ):
                
                start_time_modem = time.time()
                
                Qmi.Device.new (file, None, new_ready, None)
                # Main loop
                main_loop = GLib.MainLoop()
            
                old_pdh = device.pdh
                
                
                try:
                    main_loop.run()
                except KeyboardInterrupt:
                    if (device.pdh != None and device.pdh > 0):
                        device.state = config.STATE_DEVICE_DISCONNECT_MODE
                    else:
                        # print("\nme has roto en medio de la ejecucion")
                        #remove all dhcpc processes
                        udhcpc.stop(device.wwan_interface)
                        sys.exit(1)

                if (device.pdh != None and old_pdh == None and device.state == config.STATE_DEVICE_CONNECT_MODE):
                    device.state = config.STATE_DEVICE_DHCP_MODE

                end_time_modem = time.time()
                total_time = round(end_time_modem - start_time_modem, 4)
                #config.log.info(f"time elapsed in MAIN THREAD {total_time}")

            end_time = time.time()
            total_time = round(end_time - start_time, 4)
            exceeded_time = round(total_time-delay_seconds, 4)
            
            if (total_time > delay_seconds):
                config.log.warning(f"time defined was exceeded by {exceeded_time} seconds")
            elif (device.state == config.STATE_DEVICE_GET_INFO_MODE):
                new_time = round(delay_seconds - total_time, 4)
                config.log.debug(f"sleeping {new_time} seconds...")
                time.sleep(new_time)

    except KeyboardInterrupt:
        # print("\nme has roto en medio de la ejecucion")
        pass
    except Exception:
        pass

    device = Device.get_instance()

    if (device.pdh != None and device.pdh > 0):
        device.state = config.STATE_DEVICE_DISCONNECT_MODE
        Qmi.Device.new (file, None, new_ready, None)
        # Main loop
        main_loop = GLib.MainLoop()

        try:
            main_loop.run()
        except KeyboardInterrupt:
            # print("\nme has roto en medio de la ejecucion")
            pass

    #remove all dhcpc processes
    udhcpc.stop(device.wwan_interface)

    sys.exit()
        
        
        
