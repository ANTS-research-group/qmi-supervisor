import gi


gi.require_version('Qmi', '1.0')
from gi.repository import GLib, Gio, Qmi
from modules.services.client import Client
import config_data as config
from modules.device.device import Device
from datetime import datetime


from ctypes import *



class NASClient(Client):
    
    def __init__(self, qmidev, main_loop) -> None:
        service_type = Qmi.Service.NAS
        super().__init__(qmidev, main_loop, service_type)

    def get_nas_info(self):

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

        def get_nas_serving_system_ready(nas_qmiclient,result,qmidev):
            try:
                output = nas_qmiclient.get_serving_system_finish(result)
                
                output.get_result()

                registration_state, cs_attach_state, ps_attach_state, selected_network, radio_interfaces = output.get_serving_system()
                roaming_status = output.get_roaming_indicator()
                plmn_mcc, plmn_mnc, plmn_description = output.get_current_plmn()
                

                network_mode = []
                for radioiface in radio_interfaces:
                    network_mode.append(Qmi.NasRadioInterface.get_string(radioiface))
                    

                output_json = {
                    
                    "nas_value_serving_system_registration_state": Qmi.nas_registration_state_get_string(registration_state),
                    "nas_value_serving_system_cs_attach_state": Qmi.nas_attach_state_get_string(cs_attach_state),
                    "nas_value_serving_system_ps_attach_state": Qmi.nas_attach_state_get_string(ps_attach_state),
                    "nas_value_serving_system_selected_network": Qmi.nas_network_type_get_string(selected_network),
                    "nas_value_serving_system_radio_interfaces": network_mode,

                    "nas_value_roaming_indicator": Qmi.NasRoamingIndicatorStatus.get_string(roaming_status),

                    "nas_value_current_plmn_mcc": plmn_mcc,
                    "nas_value_current_plmn_mnc": plmn_mnc,
                    "nas_value_current_plmn_description": plmn_description
                }                    
                
                #serving_system_df = pd.DataFrame(output_json, index=[0])
                device.dynamic_info.update(output_json)
                self.update_info("serving_system", output_json)
            
                end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
                self.update_info("end_datetime", end_datetime)

            except GLib.GError as error:
                config.log.error("Couldn't get serving system information: %s\n" % error.message)

            release_client(qmidev, nas_qmiclient)
        

        def get_nas_signal_strength_ready(nas_qmiclient, result, qmidev):

            try:
                output = nas_qmiclient.get_signal_strength_finish(result)
                strength, radio_interface = output.get_signal_strength()
                io = output.get_io() #io value in dbm
                sinr = output.get_sinr() #sinr value in db
                
                match radio_interface:
                    case 0:
                        radio_interface = "no_service"
                    case 4:
                        radio_interface = "umts"
                    case 8:
                        radio_interface = "lte"
                    case 12:
                        radio_interface = "5gnr"
                    case _:
                        radio_interface = "unknown"
                
                match sinr:
                    case 0:
                        sinr = -9
                    case 1:
                        sinr = -6
                    case 2:
                        sinr = -4.5
                    case 3:
                        sinr = -3
                    case 4:
                        sinr = -2
                    case 5:
                        sinr = 1
                    case 6:
                        sinr = 3
                    case 7:
                        sinr = 6
                    case 8:
                        sinr = 9
                
                output_json = {
                    "nas_value_signal_strength_strength": strength,
                    "nas_value_signal_strength_radio_interface": radio_interface,
                    #"nas_value_io": ', '.join([str(io), "dBm"]),
                    #"nas_value_sinr": ', '.join([str(sinr), "dB"])
                    "nas_value_io": io,
                    "nas_value_sinr": sinr
                }
                
                device.dynamic_info.update(output_json)

                self.update_info("signal_strength", output_json)
            
                end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
                self.update_info("end_datetime", end_datetime)

            except GLib.GError as error:
                config.log.error("Couldn't get signal strength information: %s\n" % error.message)

            release_client(qmidev, nas_qmiclient)

        def get_nas_signal_info_ready(nas_qmiclient, result, qmidev):

            try:
                output = nas_qmiclient.get_signal_info_finish(result)

                rsrp, snr = output.get_5g_signal_strength()
                strength_extended = output.get_5g_signal_strength_extended()

                output_json = {
                    "nas_value_5g_signal_strength_rsrp": rsrp,
                    "nas_value_5g_signal_strength_snr": snr,
                    "nas_value_5g_signal_strength_extended": strength_extended
                }                    

                device.dynamic_info.update(output_json)

                self.update_info("signal_info", output_json)
            
                end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
                self.update_info("end_datetime", end_datetime)
                
            except GLib.GError as error:
                config.log.error("Couldn't get signal information: %s\n" % error.message)

            release_client(qmidev, nas_qmiclient)

        def get_nas_tx_rx_info_ready(nas_qmiclient, result, qmidev):
            try:
                output = nas_qmiclient.get_tx_rx_info_finish(result)
                
            except GLib.GError as error:
                config.log.error("Couldn't get tx and rx information: %s\n" % error.message)

            output_json = {}

            #It doesnt work on Mochabin's modem
            # try:
            #     is_radio_tunned, rx_power, ecio, rscp, rsrp, phase = output.get_rx_chain_0_info()
            #     chain_0 = {
            #         "nas_value_rx_chain_0_info_is_radio_tuned": is_radio_tunned,
            #         "nas_value_rx_chain_0_info_rx_power": rx_power,
            #         "nas_value_rx_chain_0_info_ecio": ecio,
            #         "nas_value_rx_chain_0_info_rscp": rscp,
            #         "nas_value_rx_chain_0_info_rsrp": rsrp,
            #         "nas_value_rx_chain_0_info_phase": phase

            #     }

            #     rx_chain_0_info_df = pd.DataFrame(chain_0, index=[0])
            #     device.dynamic_info_df = pd.concat([device.dynamic_info_df, rx_chain_0_info_df], axis=1)

            #     output_json.update({"rx_chain_0_info": chain_0})
            
            # except GLib.GError as error:

            #     config.log.error("Couldn't get rx (chain 0) information: %s\n" % error.message)
            
            # try:

            #     is_radio_tunned, rx_power, ecio, rscp, rsrp, phase = output.get_rx_chain_1_info()
            #     chain_1 = {
            #         "nas_value_rx_chain_1_info_is_radio_tuned": is_radio_tunned,
            #         "nas_value_rx_chain_1_info_rx_power": rx_power,
            #         "nas_value_rx_chain_1_info_ecio": ecio,
            #         "nas_value_rx_chain_1_info_rscp": rscp,
            #         "nas_value_rx_chain_1_info_rsrp": rsrp,
            #         "nas_value_rx_chain_1_info_phase": phase

            #     }

            #     rx_chain_1_info_df = pd.DataFrame(chain_1, index=[0])
            #     device.dynamic_info_df = pd.concat([device.dynamic_info_df, rx_chain_1_info_df], axis=1)

            #     output_json.update({"rx_chain_1_info": chain_1})
            
            # except GLib.GError as error:
            #     config.log.error("Couldn't get rx (chain 1) information: %s\n" % error.message)
            
            

            # try:

            #     is_radio_tunned, rx_power, ecio, rscp, rsrp, phase = output.get_rx_chain_2_info()
            #     chain_2 = {
            #         "nas_value_rx_chain_2_info_is_radio_tuned": is_radio_tunned,
            #         "nas_value_rx_chain_2_info_rx_power": rx_power,
            #         "nas_value_rx_chain_2_info_ecio": ecio,
            #         "nas_value_rx_chain_2_info_rscp": rscp,
            #         "nas_value_rx_chain_2_info_rsrp": rsrp,
            #         "nas_value_rx_chain_2_info_phase": phase

            #     }

            #     rx_chain_2_info_df = pd.DataFrame(chain_2, index=[0])
            #     device.dynamic_info_df = pd.concat([device.dynamic_info_df, rx_chain_2_info_df], axis=1)
            #     output_json.update({"rx_chain_2_info": chain_2})
            
            # except GLib.GError as error:
            #     config.log.error("Couldn't get rx (chain 2) information: %s\n" % error.message)

            # try:

            #     is_radio_tunned, rx_power, ecio, rscp, rsrp, phase = output.get_rx_chain_3_info()
            #     chain_3 = {
            #         "nas_value_rx_chain_3_info_is_radio_tuned": is_radio_tunned,
            #         "nas_value_rx_chain_3_info_rx_power": rx_power,
            #         "nas_value_rx_chain_3_info_ecio": ecio,
            #         "nas_value_rx_chain_3_info_rscp": rscp,
            #         "nas_value_rx_chain_3_info_rsrp": rsrp,
            #         "nas_value_rx_chain_3_info_phase": phase

            #     }

            #     rx_chain_3_info_df = pd.DataFrame(chain_3, index=[0])
            #     device.dynamic_info_df = pd.concat([device.dynamic_info_df, rx_chain_3_info_df], axis=1)

            #     output_json.update({"rx_chain_3_info": chain_3})
            
            # except GLib.GError as error:
            #     config.log.error("Couldn't get rx (chain 3) information: %s\n" % error.message)
            
            #it doesn't work in this modem (FIBOCOM FM160-EAU)

            # try:
            #     is_in_traffic, tx_power = output.get_tx_info()
            #     tx_info = {
            #         "nas_value_tx_info_is_in_traffic": is_in_traffic,
            #         "nas_value_tx_info_tx_power": tx_power
            #     }
                
            #     tx_info_df = pd.DataFrame(tx_info, index=[0])
            #     device.dynamic_info_df = pd.concat([device.dynamic_info_df, tx_info_df], axis=1)

            #     output_json.update({"tx_info": tx_info})
                
            # except GLib.GError as error:
            #     config.log.error("Couldn't get tx information: %s\n" % error.message)

            self.update_info("tx_rx_info", output_json)
            end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
            self.update_info("end_datetime", end_datetime)
            
            release_client(qmidev, nas_qmiclient)

        def get_nas_home_network_ready(nas_qmiclient, result, qmidev):
            
            home_network = {}

            try:
                output = nas_qmiclient.get_home_network_finish(result)

                try:
                    mcc, mnc, description = output.get_home_network()
                    network_name_source = Qmi.NasNetworkNameSource.get_string(output.get_network_name_source())

                    home_network = {
                        "nas_value_home_network_mcc": mcc,
                        "nas_value_home_network_mnc": mnc,
                        "nas_value_home_network_description": description,
                        "nas_value_network_name_source": network_name_source,
                    }

                except GLib.GError as error:
                    config.log.error("Couldn't get home network values: %s\n" % error.message)

            except GLib.GError as error:
                config.log.error("Couldn't get home network information: %s\n" % error.message)

            device.dynamic_info.update(home_network)

            self.update_info("home_network", home_network)
            end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
            self.update_info("end_datetime", end_datetime)

            release_client(qmidev, nas_qmiclient)


        def get_nas_cell_location_info_ready(nas_qmiclient, result, qmidev):

            cell_location = {}

            try:
                output = nas_qmiclient.get_cell_location_info_finish(result)
                try:
                    arfcn = output.get_nr5g_arfcn()
                    cell_information = output.get_nr5g_cell_information()

                    plmn, tac, global_cell_id, physical_cell_id, rsrq, rsrp, snr = cell_information

                    tac = (((tac[0] << 8) | tac[1]) << 8) | tac[2]

                    def str_from_bcd_plmn(bcd):
                        bcd_chars = "0123456789*#abc"
                        if not bcd or len(bcd) == 0:
                            return None
                        str_len = len(bcd) * 2
                        str_list = [''] * str_len
                        j = 0
                        for i in range(len(bcd)):
                            str_list[j] = bcd_chars[bcd[i] & 0xF]
                            if str_list[j]:
                                j += 1
                            if (bcd[i] >> 4) & 0xF < len(bcd_chars):
                                str_list[j] = bcd_chars[(bcd[i] >> 4) & 0xF]
                            if str_list[j]:
                                j += 1
                        return ''.join(str_list)
                    
                    cell_location = {
                        "nas_value_nr5g_arfcn": arfcn,
                        "nas_value_nr5g_cell_information_global_cell_id": global_cell_id,
                        "nas_value_nr5g_cell_information_physical_cell_id": physical_cell_id,
                        "nas_value_nr5g_cell_information_plmn": str_from_bcd_plmn(plmn),
                        "nas_value_nr5g_cell_information_rsrp": rsrp,
                        "nas_value_nr5g_cell_information_rsrq": rsrq,
                        "nas_value_nr5g_cell_information_snr": snr,
                        "nas_value_nr5g_cell_information_tracking_area_code": tac
                    }
                
                except GLib.GError as error:

                    config.log.error("Couldn't get cell location values: %s\n" % error.message)

                    device.retries_without_data = device.retries_without_data + 1
                    if (device.retries_without_data >= config.MAX_RETRIES_WITHOUT_DATA):
                        device.retries_without_data = 0
                        device.wwan_interface = None
                        device.state = config.STATE_DEVICE_CONNECT_MODE

                        config.log.error(f"state switch to STATE_DEVICE_CONNECT_MODE")
                        
            except GLib.GError as error:
                config.log.error("Couldn't get cell location information: %s\n" % error.message)
            
            device.dynamic_info.update(cell_location)
            
            self.update_info("cell_location", cell_location)
            end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
            self.update_info("end_datetime", end_datetime)


            release_client(qmidev, nas_qmiclient)

        def get_nas_rf_band_info_ready(nas_qmiclient, result, qmidev):

            rf_band_info = {}

            try:
                output = nas_qmiclient.get_rf_band_information_finish(result)

                try:
                    bw_list = output.get_bandwidth_list()
                    ext_list = output.get_extended_list()
                    info_list = output.get_list()
                    
                    il_radio_interface = il_active_band_class = il_active_channel = None
                    el_radio_interface = el_active_band_class = el_active_channel = None
                    bl_radio_interface = bl_bandwidth = None

                    for item in info_list:
                        if (isinstance(item, Qmi.MessageNasGetRfBandInformationOutputListElement)):
                            il_radio_interface = Qmi.NasRadioInterface.get_string(item.radio_interface)
                            il_active_band_class = Qmi.NasActiveBand.get_string(item.active_band_class)
                            il_active_channel = item.active_channel

                    for item in ext_list:
                        if (isinstance(item, Qmi.MessageNasGetRfBandInformationOutputExtendedListElement)):
                            el_radio_interface = Qmi.NasRadioInterface.get_string(item.radio_interface)
                            el_active_band_class = Qmi.NasActiveBand.get_string(item.active_band_class)
                            el_active_channel = item.active_channel
                            
                    for item in bw_list:
                        if (isinstance(item, Qmi.MessageNasGetRfBandInformationOutputBandwidthListElement)):
                            bl_radio_interface = Qmi.NasRadioInterface.get_string(item.radio_interface)
                            bl_bandwidth = Qmi.NasDLBandwidth.get_string(item.bandwidth)

                    rf_band_info = {
                        "nas_list_radio_interface": il_radio_interface,
                        "nas_list_active_band_class": il_active_band_class,
                        "nas_list_active_channel": il_active_channel,
                        "nas_extended_list_radio_interface": el_radio_interface,
                        "nas_extended_list_active_band_class": el_active_band_class,
                        "nas_extended_list_active_channel": el_active_channel,
                        "nas_bandwidth_list_radio_interface": bl_radio_interface,
                        "nas_bandwidth_list_bandwidth": bl_bandwidth,   
                    }

                except GLib.GError as error:
                    config.log.error("Couldn't get rf band info values: %s\n" % error.message)

            except GLib.GError as error:
                config.log.error("Couldn't get rf band information: %s\n" % error.message)

            device.dynamic_info.update(rf_band_info)

            self.update_info("rf_band_info", rf_band_info)
            end_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
            self.update_info("end_datetime", end_datetime)
        
            release_client(qmidev, nas_qmiclient)

        def allocate_NAS_client_ready(qmidev, result, user_data=None):

            try:
                qmi_nas_client = qmidev.allocate_client_finish(result)
                
            except GLib.GError as error:
                config.log.error("Couldn't allocate NAS QMI client: %s\n" % error.message)

                device_close(qmidev)
                if (error.code == 5):
                    device.state = config.STATE_DEVICE_RESET_MODE
                return

            set_num_requests(get_num_requests()+7)
            start_datetime = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")
            self.update_info("start_datetime", start_datetime)

            #1.-Serving System
            qmi_nas_client.get_serving_system(None, 10, None, get_nas_serving_system_ready, qmidev)

            #2.- Signal Strength
            mask = sum(config.SignalStrengthRequestFlags.values())
            input = Qmi.MessageNasGetSignalStrengthInput()
            input.set_request_mask(Qmi.NasSignalStrengthRequest(mask))
            qmi_nas_client.get_signal_strength(input, 10, None, get_nas_signal_strength_ready, qmidev)

            #3.- Signal Info
            qmi_nas_client.get_signal_info(None, 10, None, get_nas_signal_info_ready, qmidev)

            #4.- TX and RX info
            input = Qmi.MessageNasGetTxRxInfoInput()
            input.set_radio_interface(Qmi.NasRadioInterface(12)) #5GNR = 12
            qmi_nas_client.get_tx_rx_info(input, 10, None, get_nas_tx_rx_info_ready, qmidev)

            #5.- Home Network
            qmi_nas_client.get_home_network(None, 10, None, get_nas_home_network_ready, qmidev)

            #6.- Cell Location Info
            qmi_nas_client.get_cell_location_info(None, 10, None, get_nas_cell_location_info_ready, qmidev)

            #7.- RF Band Information
            qmi_nas_client.get_rf_band_information(None, 10, None, get_nas_rf_band_info_ready, qmidev)

        qmidev = self.get_qmidev()
        main_loop = self.get_main_loop()

        qmidev.allocate_client(self.service_type, Qmi.CID_NONE, 10, None, allocate_NAS_client_ready, None)
