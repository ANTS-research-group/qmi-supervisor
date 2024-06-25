
from datetime import date
import logging, shutil, time, json, os, sys, fcntl
from subprocess import Popen, PIPE
import traceback
#from gpsdclient import GPSDClient
import pandas as pd

import config_data as config
import fcntl
import socket

class Device:

    __unique_instance = object()


    def __init__(self, instance):

        try:
            assert(instance == Device.__unique_instance)
        except AssertionError as e:
            config.log.error("Device object must be created using Device.get_instance()")
            traceback.print_stack()
            sys.exit(-1)

        dynamic_metrics = {k: v for k, v in config.ListDynamicMetrics.__dict__.items() if not k.startswith('__')}
        static_metrics = {k: v for k, v in config.ListStaticMetrics.__dict__.items() if not k.startswith('__')}
        
        self.info_device = None

        self.state = config.STATE_DEVICE_CONNECT_MODE #it is offline by default when device start: OFFLINE -> CONNECT
        self.retries_to_reset = config.MAX_RETRIES_TO_RESET_DEVICE
        self.wwan_interface = None
        self.imei_notified = False
        self.clients = 0
        self.wds_cid = None
        self.apn = config.DataNetwork5GConfig.apn
        self.status_data_network = False
        self.pdh = None
        self.loc_gps = {}
        self.throughput = {}
        self.dynamic_info = dict.fromkeys(dynamic_metrics.values())
        self.static_info = dict.fromkeys(static_metrics.values())
        self.all_info = {
            "log_id": 0,
            "start_datetime": None,
            "end_datetime": None,
            "clients": {}
        }

        self.gauges = {}
        self.start_time = 0
        self.end_time = 0
        self.retries_without_data = 0
        self.retries_in_wds_info = 0

    @classmethod
    def get_instance(cls):
        if isinstance(cls.__unique_instance, Device):
            #config.log.debug("instance of Device created before")
            return cls.__unique_instance
        try:
            config.log.debug("first time instantiation of Device")
        except Exception:
            print("first time instantiation of Device")

        cls.__unique_instance = Device(cls.__unique_instance)
        return cls.__unique_instance

    def set_info_device(self, info_device: config.InfoDevice):
        self.info_device = info_device
    
    def get_info_device(self):
        return self.info_device
    
    def get_clients(self):
        return self.clients
 
    def set_clients(self, clients):
        if (clients <= 0):
            self.clients = 0
        else: 
            self.clients = clients

    def get_info(self):
        return self.all_info
    
    def set_info(self, info):
        self.all_info = info
    
    def set_end_datetime(self, end_datetime):
        self.all_info.update({"end_datetime": end_datetime})
    
    def set_start_datetime(self, start_datetime):
        self.all_info.update({"start_datetime": start_datetime})
        
    def add_info(self, client_name, client_info, start_datetime = None, end_datetime = None):

        # self.all_info["log_id"] += 1
        # self.all_info["start_datetime"] = start_datetime
        # self.all_info["end_datetime"] = end_datetime
        self.all_info["clients"].update({client_name:client_info})
    
    # Return True or False
    def check_manual(self):

        def create_usb_list_manual():
            device_list = list()
            try:
                lsusb_out = Popen('lsusb -v', shell=True, bufsize=64, stdin=PIPE, stdout=PIPE, close_fds=True).stdout.read().strip().decode('utf-8')
                usb_devices = lsusb_out.split('%s%s' % (os.linesep, os.linesep))
                for device_categories in usb_devices:
                    if not device_categories:
                        continue
                    categories = device_categories.split(os.linesep)
                    device_stuff = categories[0].strip().split()
                    bus = device_stuff[1]
                    device = device_stuff[3][:-1]
                    device_dict = {'bus': bus, 'device': device}
                    device_info = ' '.join(device_stuff[6:])
                    device_dict['description'] = device_info
                    for category in categories:
                        if not category:
                            continue
                        categoryinfo = category.strip().split()
                        if categoryinfo[0] == 'iManufacturer':
                            manufacturer_info = ' '.join(categoryinfo[2:])
                            device_dict['manufacturer'] = manufacturer_info
                        if categoryinfo[0] == 'iProduct':
                            device_info = ' '.join(categoryinfo[2:])
                            device_dict['device'] = device_info
                        if categoryinfo[0] == 'iSerial':
                            serial_number_info = ' '.join(categoryinfo[2:])
                            device_dict['serial_number'] = serial_number_info

                    path = '/dev/bus/usb/%s/%s' % (bus, device)
                    device_dict['path'] = path

                    device_list.append(device_dict)
            except Exception as ex:
                config.log.error('Failed to list usb devices! Error: %s' % ex)
                return ex
            return device_list
        
        def reset_usb_device_manual(dev_path):
            USBDEVFS_RESET = 21780
            try:
                f = open(dev_path, 'w', os.O_WRONLY)
                fcntl.ioctl(f, USBDEVFS_RESET, 0)
                return
            except Exception as ex:
                config.log.error('Failed to reset device! Error: %s' % ex)
                return ex
            
        def search_path_manual(keyword):
            usb_list = create_usb_list_manual()
            for device in usb_list:
                text = '%s %s %s %s' % (device['description'], device['manufacturer'], device['device'], device['serial_number'])
                if keyword in text:
                    return device['path']
            config.log.error('Failed to find device!')
            return None

        parameters_object = {k: v for k, v in self.__dict__.items() if not k.startswith('__')}
        parameters = {k: v for k, v in parameters_object["info_device"].__dict__.items() if not k.startswith('__')}
        dev_path = None

        if (config.GeneralConfig.reset_only_with_sn):
            config.log.debug(f'device will be only reseted if serial number exists. It was configured in GeneralConfig')

        for key_title, keyword_device in parameters.items():
            if (config.GeneralConfig.reset_only_with_sn and key_title != "serial_number"):
                break

            dev_path = search_path_manual(keyword_device)

            if (dev_path is None):
                config.log.warning(f"device was not found with '{key_title}' parameter and '{keyword_device}' value")
            else:
                config.log.debug(f"device was found with '{key_title}' parameter and '{keyword_device}' value")
                break

        if dev_path is None:
            return False
        
        try:
            config.log.debug("resetting device")
            reset_usb_device_manual(dev_path)
            config.log.debug("sleeping for 20 seconds")
            time.sleep(20)
            config.log.debug(f'device {dev_path} was reseted successfully')
            return True
        except Exception as ex:
            config.log.error('has ocurred an error while it was resetting')
            return False

    def reset_info_structs(self):
        
        config.log.debug(self.dynamic_info)
        
        self.all_info = {
            "log_id": 0,
            "start_datetime": None,
            "end_datetime": None,
            "clients": {}
        }

        self.dynamic_info_df = pd.DataFrame()
        self.static_info_df = pd.DataFrame()
        dynamic_metrics = {k: v for k, v in config.ListDynamicMetrics.__dict__.items() if not k.startswith('__')}
        self.dynamic_info = dict.fromkeys(dynamic_metrics.values())

    def create_dir_struct(self):

        def copy_file_and_delete(source_file_path, destination_file_path, handlers=None, new_filename=None):
            with open(source_file_path, 'r') as source_file:
                source_content = source_file.read()

            with open(destination_file_path, 'a') as destination_file:
                destination_file.write('\n' + source_content)

            if handlers and new_filename:
                update_handler_base_filename(handlers, new_filename)

            # Cerrar el archivo de origen antes de eliminarlo
            source_file.close()

            # Renombrar el archivo de destino para evitar el bloqueo de la lectura
            temp_file_path = destination_file_path + '.tmp'
            os.rename(destination_file_path, temp_file_path)
            os.rename(temp_file_path, destination_file_path)

            # Actualizar el archivo de destino en el handler
            if handlers and new_filename:
                for handler in handlers:
                    if isinstance(handler, logging.FileHandler):
                        handler.baseFilename = destination_file_path
                        handler.stream = open(destination_file_path, 'a+')

            # Eliminar el archivo de origen
            os.remove(source_file_path)


        def update_handler_base_filename(handlers, new_filename):
            for handler in handlers:
                if isinstance(handler, logging.FileHandler):
                    handler.baseFilename = new_filename

        try:
            imei = self.static_info["dms_value_imei"]
        except Exception:
            config.log.debug("IMEI wasn't found in static_info dict")
            return False
        
        if (imei is None):
            config.log.debug("IMEI wasn't found in static_info dict")
            return False
        
        dir_path = config.LoggingConfig.log_dir_path
        
        try:
            if not os.path.exists(f'{dir_path}'):
                os.makedirs(f'{dir_path}/')

        except Exception:
            config.log.error("struct of dir_path couldn't created")
            return False
    
        try:
            if not os.path.exists(f'{dir_path}/modem_{imei}'):
                os.makedirs(f'{dir_path}/modem_{imei}')

        except Exception:
            config.log.error(f"struct of modem_{imei} couldn't created")
            return False

        origin_log_file_path = f'/tmp/{os.path.basename(self.info_device.device_path)}_{config.LoggingConfig.log_file_path}'
        destination_log_file_path = f'{dir_path}/modem_{imei}/{os.path.basename(self.info_device.device_path)}_{config.LoggingConfig.log_file_path}'

        # check if files exists
        if os.path.exists(origin_log_file_path) and os.path.exists(destination_log_file_path):
            copy_file_and_delete(origin_log_file_path, destination_log_file_path, handlers=config.log.handlers, new_filename=destination_log_file_path)

        elif os.path.exists(origin_log_file_path) and not os.path.exists(destination_log_file_path):
            shutil.move(origin_log_file_path, destination_log_file_path)
            update_handler_base_filename(config.log.handlers, destination_log_file_path)

        return True

    def save_to_csv(self):

        def has_header(file: str, nrows=2) -> bool: 
            try:
                df = pd.read_csv(file, header=None, nrows=nrows)
            except Exception as e:
                return False
            df_header = pd.read_csv(file, nrows=nrows)
            return (tuple(df.dtypes) != tuple(df_header.dtypes))
        
        
        
        try:
            imei = self.static_info["dms_value_imei"]
        except Exception:
            config.log.error("IMEI wasn't found in static_info dict")
            return
    
        if (imei is None):
            config.log.error("IMEI wasn't found in static_info dict")
            return
        
        dir_path = f'{config.LoggingConfig.log_dir_path}/modem_{imei}'
        log_dynamic_stats_file_path_csv = config.LoggingConfig.log_dynamic_stats_file_path_csv
        log_static_stats_file_path_csv = config.LoggingConfig.log_static_stats_file_path_csv

        today = date.today().strftime("%Y%m%d")  # Obtiene la fecha actual en formato AAAAMMDD 
        tmp_file_name, file_ext = os.path.splitext(log_dynamic_stats_file_path_csv)
        log_dynamic_stats_file_path_csv = f'{dir_path}/{tmp_file_name}_{today}{file_ext}'

        tmp_file_name, file_ext = os.path.splitext(log_static_stats_file_path_csv)
        log_static_stats_file_path_csv = f'{dir_path}/{tmp_file_name}_{today}{file_ext}'

        is_empty_dynamic = all(not bool(valor) for valor in self.dynamic_info.values())
        is_empty_static = all(not bool(valor) for valor in self.static_info.values())

        if (not is_empty_dynamic):
            
            # create csv file if not exists
            if not os.path.exists(log_dynamic_stats_file_path_csv):
                try:
                    with open(log_dynamic_stats_file_path_csv, 'w') as archivo:
                        archivo.write("")
                        config.log.debug(f'file {log_dynamic_stats_file_path_csv} not found so it was created')
                except:
                    config.log.error(f'has ocurrred an error while file {log_dynamic_stats_file_path_csv} was creating')
                    return
            
            dynamic_info_df = pd.DataFrame(self.dynamic_info, index=[0])

            if (has_header(log_dynamic_stats_file_path_csv)):
                dynamic_info_df.to_csv(log_dynamic_stats_file_path_csv, mode='a',  index=False, header=False)
            else:
                dinamyc_list = {k: v for k, v in config.ListDynamicMetrics.__dict__.items() if not k.startswith('__')}
                dynamic_info_df.to_csv(log_dynamic_stats_file_path_csv, mode='a', index=False, header=dinamyc_list.values())
            
            config.log.debug("new dynamic data saved into csv file")

        if (not is_empty_static):
            # create csv file if not exists
            if not os.path.exists(log_static_stats_file_path_csv):
                try:
                    with open(log_static_stats_file_path_csv, 'w') as archivo:
                        archivo.write("")
                        config.log.debug(f'file {log_static_stats_file_path_csv} not found so it was created')
                except:
                    config.log.error(f'has ocurrred an error while file {log_static_stats_file_path_csv} was creating')
                    return
                
                static_info_df = pd.DataFrame(self.static_info, index=[0])
            
                if (has_header(log_static_stats_file_path_csv)):
                    static_info_df.to_csv(log_static_stats_file_path_csv, mode='a',  index=False, header=False)
                else:
                    static_list = {k: v for k, v in config.ListStaticMetrics.__dict__.items() if not k.startswith('__')}
                    static_info_df.to_csv(log_static_stats_file_path_csv, mode='a', index=False, header=static_list.values())
                
                config.log.debug("new static data saved into csv file")
                
    def save_to_json(self):

        imei = self.static_info[config.ListStaticMetrics.DMS_VALUE_IMEI]

        dir_path = f'{config.LoggingConfig.log_dir_path}/modem_{imei}'
        log_stats_file_path_json = f'{dir_path}/{config.LoggingConfig.log_stats_file_path_json}'

        df = pd.DataFrame(self.all_info)
        df.to_json(log_stats_file_path_json, indent=3)

        config.log.debug("all data saved into json file")
        # config.log.debug(self.all_info)

    def send_to_broadcast(self):
        
        
        info_json = json.dumps(self.all_info)
        socket_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)  # UDP
        socket_udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        socket_udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        ip_udp = config.NetworkConfig.ip_broadcast_udp
        port_udp = int(config.NetworkConfig.port_broadcast_udp)
        socket_udp.sendto(bytes(info_json, "utf-8"), (ip_udp, port_udp))

        config.log.debug("new data sent to broadcast")

    def send_to_prometheus_collector(self):

        data_to_send = {}
        data_to_send["static"] = self.static_info
        data_to_send["dynamic"] = self.dynamic_info
        data_to_send = json.dumps(data_to_send)

        socket_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)  # UDP
        socket_udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        ip_udp = config.NetworkConfig.ip_prometheus_collector
        port_udp = int(config.NetworkConfig.port_prometheus_collector)
        socket_udp.sendto(bytes(data_to_send, "utf-8"), (ip_udp, port_udp))
        
        config.log.debug("new data sent to prometheus module")

        # closing the socket
        socket_udp.close()

    def get_gpsd_location(self):


        self.add_info(client_name="gpsd", client_info=self.loc_gps)

        for key in self.loc_gps.keys():

            attributes_gps = self.loc_gps[key]

            for field_name in attributes_gps.keys():

                self.dynamic_info.update({f'gpsd_{key}_{field_name}': attributes_gps[field_name]})

        #config.log.debug(f"info gps: {self.loc_gps}")
    
    def get_throughput(self):

        self.add_info(client_name="throughput", client_info=self.throughput)

        self.dynamic_info.update(self.throughput)

        #config.log.debug(f"info throughput: {self.throughput}")
        