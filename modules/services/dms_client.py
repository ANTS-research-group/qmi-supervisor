import gi
gi.require_version('Qmi', '1.0')

from gi.repository import GLib, Gio, Qmi
from modules.services.client import Client
import config_data as config
from modules.device.device import Device
from datetime import datetime

class DMSClient(Client):

    def __init__(self, qmidev, main_loop) -> None:
        
        service_type = Qmi.Service.DMS
        super().__init__(qmidev, main_loop, service_type)
        
    def get_dms_info(self): 
        
        device = Device.get_instance()

        def get_cid():
            return self.get_cid()
        
        def get_num_requests():
            return self.get_num_requests()
        
        def set_num_requests(num_requests):
            self.set_num_requests(num_requests)
        

        def device_close_ready(qmidev,result,user_data=None):
            
            try:
                qmidev.close_finish(result)
            except GLib.GError as error:
                config.log.error("Couldn't close QMI device: %s\n" % error.message)

            end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
            device.set_end_datetime(end_datetime)
            main_loop.quit()

        def device_close(qmidev):

            device.set_clients(device.get_clients()-1)
            config.log.debug("dms_num_requests: %i" % get_num_requests())
            config.log.debug("clients: %i" % device.get_clients())
            if (get_num_requests() == 0 and device.get_clients() == 0):
                qmidev.close_async(10, None, device_close_ready, None)

        def release_client_ready(qmidev,result,user_data=None):
            try:
                qmidev.release_client_finish(result)
            except GLib.GError as error:
                config.log.error("Couldn't release DMS QMI client: %s\n" % error.message)
            device_close(qmidev)

        def release_client(qmidev,qmiclient):
            
            set_num_requests(get_num_requests()-1)
            if (get_num_requests() == 0):
                device.add_info(client_name=Qmi.service_get_string(self.service_type), client_info=self.info)
                qmidev.release_client(qmiclient, Qmi.DeviceReleaseClientFlags.RELEASE_CID, 10, None, release_client_ready, None)

        def get_ids_ready(qmiclient,result,qmidev):
            
            ids = {}

            try:
                output = qmiclient.get_ids_finish(result)
                output.get_result()
  
                try:
                    imei = {"dms_value_imei": str(output.get_imei())}
                    ids.update(imei)
                    if (device.imei_notified == False):
                        config.log.info(f'Device IMEI: {output.get_imei()}')
                        device.imei_notified = True

                except:
                    config.log.error("couldn't query device imei")
                    pass

                # try:
                #     imei_software_version = {"dms_value_imei_software_version": int(output.get_imei_software_version())}
                #     ids.update(imei_software_version)
                # except:
                #     config.log.error("couldn't query device imei software version")
                #     pass

                try:
                    meid = {"dms_value_meid": int(output.get_meid())}
                    ids.update(meid)
                except Exception as e:
                    #config.log.error("couldn't query device meid %s" % e)
                    pass

                try:
                    esn = {"dms_value_esn": int(output.get_esn())}
                    ids.update(esn)
                except:
                    #config.log.error("couldn't query device dms_esn %s" % e)
                    pass

            except GLib.GError as error:
                config.log.error("Couldn't query device ids: %s\n" % error.message)
                release_client(qmidev, qmiclient)
                return
            
            device.static_info.update(ids)

            self.update_info("ids", ids)
            
            end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
            self.update_info("end_datetime", end_datetime)
            
            release_client(qmidev, qmiclient)

        def get_capabilities_ready(qmiclient,result,qmidev):
            
            output_json = {}
            try:
                output = qmiclient.get_capabilities_finish(result)
                output.get_result()

                maxtxrate, maxrxrate, dataservicecaps, simcaps, radioifaces = output.get_info()
                
    
                networks = []
                for radioiface in radioifaces:
                    radioiface_name = Qmi.DmsRadioInterface.get_string(radioiface)
                    if (radioiface_name is None):
                        continue
                    networks.append(radioiface_name)

                output_json = {
                    "dms_value_info_max_tx_channel_rate": maxtxrate,
                    "dms_value_info_max_rx_channel_rate": maxrxrate,
                    "dms_value_info_data_service_capability": Qmi.DmsDataServiceCapability.get_string(dataservicecaps),
                    "dms_value_info_sim_capability": Qmi.DmsSimCapability.get_string(simcaps),
                    "dms_value_info_radio_interface_list": ', '.join(networks)
                }

            except GLib.GError as error:
                config.log.error("Couldn't query device capabilities: %s\n" % error.message)

            
            device.static_info.update(output_json)

            self.update_info("capabilities", output_json)

            end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
            self.update_info("end_datetime", end_datetime)
            release_client(qmidev, qmiclient)
            
        def allocate_client_ready(qmidev,result,user_data=None):

            try:
                qmiclient = qmidev.allocate_client_finish(result)
            except GLib.GError as error:
                config.log.error("Couldn't allocate DMS QMI client: %s\n" % error.message)

                device_close(qmidev)
                if (error.code == 5):
                    device.state = config.STATE_DEVICE_RESET_MODE

                return
            
            set_num_requests(get_num_requests()+2)

            start_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
            self.update_info("start_datetime", start_datetime)

            qmiclient.get_capabilities(None, 10, None, get_capabilities_ready, qmidev)
            qmiclient.get_ids(None, 10, None, get_ids_ready, qmidev)

        main_loop = self.get_main_loop()
        qmidev = self.get_qmidev()

        qmidev.allocate_client(self.service_type, Qmi.CID_NONE, 10, None, allocate_client_ready, None)

