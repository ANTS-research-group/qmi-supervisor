import gi
from configparser import ConfigParser
gi.require_version('Qmi', '1.0')
from gi.repository import GLib, Gio, Qmi
from modules.device.device import Device
import config_data as config

class Client():
    

    num_requests = 0

    def __init__(self, qmidev, main_loop, service_type) -> None:
        
        self.log = config.log

        self.cid = 0
        self.qmidev = qmidev
        self.main_loop = main_loop
        self.service_type = service_type
        self.info = {
            "start_datetime": None,
            "end_datetime": None,
            "info": {}

        }

        device = Device.get_instance()
        device.set_clients(device.get_clients()+1)
        
        self.log.debug("new {} client - ID: {} ".format(Qmi.service_get_string(self.service_type).upper(), device.get_clients()))

    #getters
    def get_cid(self):
        return self.cid
    
    def set_cid(self, cid):
        self.cid = cid

    def get_device(self):
        return self.device

    def get_qmidev(self):
        return self.qmidev
    
    def get_main_loop(self):
        return self.main_loop
    
    def get_num_requests(self):
        return self.num_requests

    def set_num_requests(self, num_requests):
        self.num_requests = num_requests
        
    def get_info(self):
        return self.info
    
    def set_info(self, info):
        self.info = info
    
    #insert if no exist
    def update_info(self, key, value):
        
        if (key in self.info):
            self.info.update({key:value})
        else:
        #self.info.update({key:value})
            self.info["info"].update({key:value})
        
    def init(self):

        device = Device.get_instance()

        def sub_clients():
            
            device.set_clients(device.get_clients()-1)

        def device_close_ready(qmidev,result,user_data=None):
            
            try:
                qmidev.close_finish(result)
            except GLib.GError as error:
                config.log.error("Couldn't close QMI device: %s\n" % error.message)
            main_loop.quit()

        def device_close(qmidev):

            qmidev.close_async(10, None, device_close_ready, None)

        def release_client_ready(qmidev,result,user_data=None):
            
            sub_clients()

            try:
                qmidev.release_client_finish(result)
            except GLib.GError as error:
                config.log.error("Couldn't release QMI client: %s\n" % error.message)

        def release_client(qmidev,qmiclient):
            
            qmidev.release_client(qmiclient, Qmi.DeviceReleaseClientFlags.NONE, 10, None, release_client_ready, None)

        def set_cid(cid):
            self.cid = cid
        
        def device_close_ready(qmidev, result, user_data=None):
            try:
                qmidev.close_finish(result)
            except GLib.GError as error:
                config.log.error("Couldn't close QMI device: %s" % error.message)
            main_loop.quit()

        def allocate_client_ready(qmidev,result,user_data=None):

            try:
                qmiclient = qmidev.allocate_client_finish(result)

            except GLib.GError as error:
                
                config.log.error(f"Couldn't allocate {service_type} QMI client: {error.message}")
                
                device_close(qmidev)
                if (error.code == 5):
                    device.state = config.STATE_DEVICE_RESET_MODE

                return
            
            device.state = config.STATE_DEVICE_CONNECT_MODE

            cid =  qmiclient.get_cid()
            if self.cid != cid:
                set_cid(cid)
                
            release_client(qmidev, qmiclient)

        service_type = self.service_type
        main_loop = self.get_main_loop()
        qmidev = self.get_qmidev()

        qmidev.allocate_client(self.service_type, Qmi.CID_NONE, 10, None, allocate_client_ready, None)
