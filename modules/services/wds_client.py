from datetime import datetime
import gi

import pandas as pd

gi.require_version('Qmi', '1.0')
from gi.repository import GLib, Gio, Qmi, GObject
from modules.services.client import Client
import config_data as config
from modules.device.device import Device
import udhcpc

class WDSClient(Client):
    
    def __init__(self, qmidev, main_loop) -> None:
        service_type = Qmi.Service.WDS
        super().__init__(qmidev, main_loop, service_type)

    def get_wds_info(self):

        device = Device.get_instance()

        def get_num_requests():
            return self.get_num_requests()
        
        def set_num_requests(num_requests):
            self.set_num_requests(num_requests)

        def device_close_ready(qmidev,result,user_data=None):
            device = Device.get_instance()
            device.set_clients(device.get_clients()-1)
            try:
                qmidev.close_finish(result)
            except GLib.GError as error:
                config.log.error("Couldn't close QMI device: %s\n" % error.message)
            
            end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
            device.set_end_datetime(end_datetime)
            main_loop.quit()

        def device_close(qmidev):
            device = Device.get_instance()
            device.set_clients(device.get_clients()-1)
            config.log.debug("num_requests: %i" % get_num_requests())
            config.log.debug("clients: %i" % device.get_clients())
            if (get_num_requests() == 0 and device.get_clients() == 0):
                qmidev.close_async(10, None, device_close_ready, None)

        def release_client_ready(qmidev,result,user_data=None):
            try:
                qmidev.release_client_finish(result)
            except GLib.GError as error:
                config.log.error("Couldn't release QMI client: %s\n" % error.message)
            device_close(qmidev)

        def release_client(qmidev, qmiclient):
            set_num_requests(get_num_requests()-1)
            if (get_num_requests() == 0):
                device.add_info(client_name=Qmi.service_get_string(self.service_type), client_info=self.info)
                qmidev.release_client(qmiclient, Qmi.DeviceReleaseClientFlags.NONE, 10, None, release_client_ready, None)

        def get_wds_packet_statistics_ready(wds_client, result, qmidev):
            
            try:
                output = wds_client.get_packet_statistics_finish(result)
                
                output_json = {
                    "wds_value_tx_packets_ok":  output.get_tx_packets_ok(),
                    "wds_value_rx_packets_ok":  output.get_rx_packets_ok(),
                    "wds_value_tx_packets_error":  output.get_tx_packets_error(),
                    "wds_value_rx_packets_error":  output.get_rx_packets_error(),
                    "wds_value_tx_overflows":  output.get_tx_overflows(),
                    "wds_value_rx_overflows":  output.get_rx_overflows(),
                    "wds_value_tx_bytes_ok":  output.get_tx_bytes_ok(),
                    "wds_value_rx_bytes_ok":  output.get_rx_bytes_ok(),
                    "wds_value_tx_packets_dropped":  output.get_tx_packets_dropped(),
                    "wds_value_rx_packets_dropped":  output.get_rx_packets_dropped()
                }

                device.dynamic_info.update(output_json)

                self.update_info("packet_statistics", output_json)
                end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
                self.update_info("end_datetime", end_datetime)

            except GLib.GError as error:
                config.log.error("Couldn't get packet statistics: %s" % error.message)
                #error timeout
                if (error.code == 2):
                    device.retries_in_wds_info = device.retries_in_wds_info + 1

            release_client(qmidev, wds_client)

        def get_wds_channel_rates_ready(wds_client, result, qmidev):
            try:
                output = wds_client.get_channel_rates_finish(result)
                channel_tx_rate_bps, channel_rx_rate_bps, max_channel_tx_rate_bps, max_channel_rx_rate_bps = output.get_channel_rates()

                output_json = {
                    "wds_value_channel_rates_channel_tx_rate_bps":  channel_tx_rate_bps,
                    "wds_value_channel_rates_channel_rx_rate_bps":  channel_rx_rate_bps,
                    "wds_value_channel_rates_max_channel_tx_rate_bps":  max_channel_tx_rate_bps,
                    "wds_value_channel_rates_max_channel_rx_rate_bps":  max_channel_rx_rate_bps
                }

                device.dynamic_info.update(output_json)

                self.update_info("channel_rates", output_json)
                end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
                self.update_info("end_datetime", end_datetime)

            except GLib.GError as error:
                config.log.error("Couldn't get channel rates information: %s" % error.message)

                #error timeout
                if (error.code == 2):
                    device.retries_in_wds_info = device.retries_in_wds_info + 1

 
            release_client(qmidev, wds_client)

        def allocate_wds_client_ready(qmidev, result, user_data=None):

            try:
                wds_client = qmidev.allocate_client_finish(result)
            except GLib.GError as error:
                config.log.error("Couldn't allocate WDS QMI client: %s\n" % error.message)
                
                device_close(qmidev)
                if (error.code == 5):
                    device.state = config.STATE_DEVICE_RESET_MODE

                return
            

            set_num_requests(get_num_requests()+2)
            start_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
            self.update_info("start_datetime", start_datetime)

            #1.- Packet Statistics
            new_mask = sum(config.PacketStatisticsMaskFlags.values())
            input_value = Qmi.MessageWdsGetPacketStatisticsInput()
            input_value.set_mask(Qmi.WdsPacketStatisticsMaskFlag(new_mask))
            wds_client.get_packet_statistics(input_value, 10, None, get_wds_packet_statistics_ready, qmidev)

            #2.- Channel Rates
            wds_client.get_channel_rates(None, 10, None, get_wds_channel_rates_ready, qmidev)

        qmidev = self.get_qmidev()
        main_loop = self.get_main_loop()
        
        if (device.wds_cid == None):
            cid = Qmi.CID_NONE
        else:
            cid = device.wds_cid

        qmidev.allocate_client(self.service_type, cid, 10, None, allocate_wds_client_ready, None)
    
    def wds_start_network(self):

        device = Device.get_instance()

        def get_num_requests():
            return self.get_num_requests()
        
        def set_num_requests(num_requests):
            self.set_num_requests(num_requests)

        def device_close_ready(qmidev,result,user_data=None):
            device = Device.get_instance()
            device.set_clients(device.get_clients()-1)
            try:
                qmidev.close_finish(result)
            except GLib.GError as error:
                config.log.error("Couldn't close QMI device: %s\n" % error.message)
            
            end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
            device.set_end_datetime(end_datetime)
            main_loop.quit()

        def device_close(qmidev):
            device = Device.get_instance()
            device.set_clients(device.get_clients()-1)
            config.log.debug("num_requests: %i" % get_num_requests())
            config.log.debug("clients: %i" % device.get_clients())
            if (get_num_requests() == 0 and device.get_clients() == 0):
                qmidev.close_async(10, None, device_close_ready, None)

        def release_client_ready(qmidev,result,user_data=None):
            try:
                qmidev.release_client_finish(result)
            except GLib.GError as error:
                config.log.error("Couldn't release QMI client: %s\n" % error.message)
                device.wds_cid = None
            device_close(qmidev)

        def release_client(qmidev, qmiclient):
            set_num_requests(get_num_requests()-1)
            if (get_num_requests() == 0):
                device.add_info(client_name=Qmi.service_get_string(self.service_type), client_info=self.info)
                qmidev.release_client(qmiclient, Qmi.DeviceReleaseClientFlags.NONE, 10, None, release_client_ready, None)

        def start_network_finish(wds_client, result, qmidev):

            config.log.info("starting network...") 
            try:
                output = wds_client.start_network_finish(result)
                result = output.get_result()
                pdh = output.get_packet_data_handle()

                device.pdh = pdh

                config.log.info(f"Packet Data Handle generated: {pdh}")

                output_json = {
                    'pdh': pdh,
                    'cid': wds_client.get_cid()
                }

                device.static_info.update(output_json)

                self.update_info("start_network", output_json)
                end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
                self.update_info("end_datetime", end_datetime)

            except GLib.GError as error:
                config.log.warning("Couldn't start network: %s" % error.message)

                # Code 26: NoEffect: data network connection is established already
                if error.code == 26:
                    device.state = config.STATE_DEVICE_CHECK_IS_CONNECTED
                else: 
                    if (config.GeneralConfig.connect_to_network):
                        device.state = config.STATE_DEVICE_DISCONNECTED_MODE
                pass

            release_client(qmidev, wds_client)

        def allocate_wds_client_ready(qmidev, result, user_data=None):

            try:
                wds_client = qmidev.allocate_client_finish(result)
                new_cid = wds_client.get_cid()
                config.log.info(f"new cid allocated: {new_cid}")
                device.wds_cid = wds_client.get_cid()
            except GLib.GError as error:
                config.log.error("Couldn't allocate WDS QMI client: %s\n" % error.message)
                
                device_close(qmidev)
                if (error.code == 5):
                    device.state = config.STATE_DEVICE_RESET_MODE

                return
            
            # Release start_network +1 additional to use in stop_network
            set_num_requests(get_num_requests()+1)
            start_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
            self.update_info("start_datetime", start_datetime)

            #1.- Start network
            input = Qmi.MessageWdsStartNetworkInput()
            input.set_apn(config.DataNetwork5GConfig.apn)
            input.set_ip_family_preference(config.DataNetwork5GConfig.ip_type)
            wds_client.start_network(input, 10, None, start_network_finish, qmidev)

        qmidev = self.get_qmidev()
        main_loop = self.get_main_loop()
        
        qmidev.allocate_client(self.service_type, Qmi.CID_NONE, 10, None, allocate_wds_client_ready, None)
        #set_ip_family
        #start_network
        #client-no-release
    
    def wds_stop_network(self):
        device = Device.get_instance()

        def get_num_requests():
            return self.get_num_requests()
        
        def set_num_requests(num_requests):
            self.set_num_requests(num_requests)

        def device_close_ready(qmidev,result,user_data=None):
            device = Device.get_instance()
            device.set_clients(device.get_clients()-1)
            try:
                qmidev.close_finish(result)
            except GLib.GError as error:
                config.log.error("Couldn't close QMI device: %s\n" % error.message)
            
            end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
            device.set_end_datetime(end_datetime)
            main_loop.quit()

        def device_close(qmidev):
            device = Device.get_instance()
            device.set_clients(device.get_clients()-1)
            config.log.debug("num_requests: %i" % get_num_requests())
            config.log.debug("clients: %i" % device.get_clients())
            if (get_num_requests() == 0 and device.get_clients() == 0):
                qmidev.close_async(10, None, device_close_ready, None)

        def release_client_ready(qmidev,result,user_data=None):
            try:
                qmidev.release_client_finish(result)
            except GLib.GError as error:
                config.log.error("Couldn't release QMI client: %s\n" % error.message)
            device_close(qmidev)

        def release_client(qmidev, qmiclient):
            set_num_requests(get_num_requests()-1)
            if (get_num_requests() == 0):
                device.add_info(client_name=Qmi.service_get_string(self.service_type), client_info=self.info)
                qmidev.release_client(qmiclient, Qmi.DeviceReleaseClientFlags.RELEASE_CID, 10, None, release_client_ready, None)
        
        def stop_network_finish(wds_client, result, qmidev):
            try:
                output = wds_client.stop_network_finish(result)

                result = output.get_result()
                config.log.error(result)
                if (result):
                    config.log.info("the network was stopped successfully")
                    device.pdh = None
                else:
                    config.log.error("an error has ocurrred while it was trying disconnect the data network")
                
                config.log.info("stop_network function executed successfully")

                output_json = {"stop_network": result}

                device.static_info.update(output_json)

                self.update_info("stop_network", output_json)
                end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
                self.update_info("end_datetime", end_datetime)

            except GLib.GError as error:
                config.log.error("Couldn't stop network: %s" % error.message)

            device.state = config.STATE_DEVICE_DISCONNECTED_MODE
            udhcpc.stop(device.wwan_interface)
            
            release_client(qmidev, wds_client)
        
        def allocate_wds_client_ready(qmidev, result, user_data=None):

            try:
                wds_client = qmidev.allocate_client_finish(result)

            except GLib.GError as error:
                config.log.error("Couldn't allocate WDS QMI client: %s\n" % error.message)
                
                device_close(qmidev)
                if (error.code == 5):
                    device.state = config.STATE_DEVICE_RESET_MODE

                return

            set_num_requests(get_num_requests()+1)
            start_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
            self.update_info("start_datetime", start_datetime)
            
            #1.- Stop Network
            input = Qmi.MessageWdsStopNetworkInput()
            input.set_packet_data_handle(device.pdh)
            input.set_disable_autoconnect(True)
            wds_client.stop_network(input, 10, None, stop_network_finish, qmidev)

        qmidev = self.get_qmidev()
        main_loop = self.get_main_loop()
        
        cid = int(device.wds_cid)
        qmidev.allocate_client(self.service_type, cid, 10, None, allocate_wds_client_ready, None)

    def wds_packet_service_status(self):

        device = Device.get_instance()

        def get_num_requests():
            return self.get_num_requests()
        
        def set_num_requests(num_requests):
            self.set_num_requests(num_requests)

        def device_close_ready(qmidev,result,user_data=None):
            device = Device.get_instance()
            device.set_clients(device.get_clients()-1)
            try:
                qmidev.close_finish(result)
            except GLib.GError as error:
                config.log.error("Couldn't close QMI device: %s\n" % error.message)
            
            end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
            device.set_end_datetime(end_datetime)
            main_loop.quit()

        def device_close(qmidev):
            device = Device.get_instance()
            device.set_clients(device.get_clients()-1)
            config.log.debug("num_requests: %i" % get_num_requests())
            config.log.debug("clients: %i" % device.get_clients())
            if (get_num_requests() == 0 and device.get_clients() == 0):
                qmidev.close_async(10, None, device_close_ready, None)

        def release_client_ready(qmidev,result,user_data=None):
            try:
                qmidev.release_client_finish(result)
            except GLib.GError as error:
                config.log.error("Couldn't release QMI client: %s\n" % error.message)
                device.wds_cid = None
            device_close(qmidev)

        def release_client(qmidev, qmiclient):
            set_num_requests(get_num_requests()-1)
            if (get_num_requests() == 0):
                device.add_info(client_name=Qmi.service_get_string(self.service_type), client_info=self.info)
                qmidev.release_client(qmiclient, Qmi.DeviceReleaseClientFlags.NONE, 10, None, release_client_ready, None)

        def get_packet_service_status_finish(wds_client, result, qmidev):
            try:
                output = wds_client.get_packet_service_status_finish(result)

                try:
                    status_value = output.get_connection_status()

                    output_json = {
                        config.ListDynamicMetrics.WDS_VALUE_CONNECTION_STATUS:  Qmi.WdsConnectionStatus.get_string(status_value)
                    }

                    if device.state == config.STATE_DEVICE_CHECK_IS_CONNECTED and status_value == Qmi.WdsConnectionStatus.CONNECTED:
                        device.state = config.STATE_DEVICE_CONNECTED_MODE
                    elif device.state == config.STATE_DEVICE_CHECK_IS_DISCONNECTED and status_value == Qmi.WdsConnectionStatus.DISCONNECTED:
                        device.state = config.STATE_DEVICE_DISCONNECTED_MODE
                    else:
                        device.state = config.STATE_DEVICE_DISCONNECTED_MODE

                    # device.dynamic_info.update(output_json)

                    # self.update_info("packet_service_status", output_json)
                    # end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
                    # self.update_info("end_datetime", end_datetime)

                except GLib.GError as error:
                    config.log.error("Couldn't get packet service status value: %s" % error.message)

            except GLib.GError as error:
                config.log.error("Couldn't get packet service status: %s\n" % error.message)
 
            release_client(qmidev, wds_client)

        def allocate_wds_client_ready(qmidev, result, user_data=None):

            try:
                wds_client = qmidev.allocate_client_finish(result)
                new_cid = wds_client.get_cid()
                config.log.info(f"new cid allocated: {new_cid}")
                device.wds_cid = wds_client.get_cid()
            except GLib.GError as error:
                config.log.error("Couldn't allocate WDS QMI client: %s\n" % error.message)
                
                device_close(qmidev)
                if (error.code == 5):
                    device.state = config.STATE_DEVICE_RESET_MODE

                return
            
            # Release start_network +1 additional to use in stop_network
            set_num_requests(get_num_requests()+1)
            # start_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
            # self.update_info("start_datetime", start_datetime)

            #1.- Get Packet Service Status
            wds_client.get_packet_service_status(None, 10, None, get_packet_service_status_finish, qmidev)
            
        qmidev = self.get_qmidev()
        main_loop = self.get_main_loop()
        
        if (device.wds_cid == None):
            cid = Qmi.CID_NONE
        else:
            cid = device.wds_cid
        
        qmidev.allocate_client(self.service_type, cid, 10, None, allocate_wds_client_ready, None)

    def config_profile(self):
        pass
