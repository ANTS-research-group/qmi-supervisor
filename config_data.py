""" Config Logging """

parser = None
log = None
args = None

""" QMI Parameters """

PacketStatisticsMaskFlags = {
    'TX_PACKETS_OK': 1,
    'RX_PACKETS_OK': 2,
    'TX_PACKETS_ERROR': 4,
    'RX_PACKETS_ERROR': 8,
    'TX_OVERFLOWS': 16,
    'RX_OVERFLOWS': 32,
    'TX_BYTES_OK': 64,
    'RX_BYTES_OK': 128,
    'TX_PACKETS_DROPPED': 256,
    'RX_PACKETS_DROPPED': 512   
}

SignalStrengthRequestFlags = {
    'NONE': 0,
    'RSSI': 1,
    'ECIO': 2,
    'IO': 4,
    'SINR':8,
    'ERROR_RATE': 16,
    'RSRQ': 32,
    'LTE_SNR': 64,
    'LTE_RSRP':128
}


IpFamilyEnum = {
    'UNKNOWN': 0,
    'IPV4': 4,
    'IPV6': 6,
    'UNSPECIFIED': 8
}

"""" State Machine """

STATE_DEVICE_RESET_MODE = 0
STATE_DEVICE_DISCONNECTED_MODE = 1
STATE_DEVICE_CONNECT_MODE = 2
STATE_DEVICE_CONNECTED_MODE = 3
STATE_DEVICE_CHECK_IS_CONNECTED = 4
STATE_DEVICE_CHECK_IS_DISCONNECTED = 5
STATE_DEVICE_CHECK_PACKET_SERVICE_MODE = 6
STATE_DEVICE_GET_INFO_MODE = 7
STATE_DEVICE_DISCONNECT_MODE = 8
STATE_DEVICE_DHCP_MODE = 9

MAX_RETRIES_WITHOUT_DATA = 15
MAX_RETRIES_TO_RESET_DEVICE = 15
MAX_RETRIES_IN_WDS_INFO = 15

class ListStaticMetrics:
    DMS_VALUE_IMEI = 'dms_value_imei'
    DMS_VALUE_IMEI_SOFTWARE_VERSION = 'dms_value_imei_software_version'
    DMS_VALUE_MEID = 'dms_value_meid'
    DMS_VALUE_ESN = 'dms_value_esn'
    DMS_VALUE_INFO_MAX_TX_CHANNEL_RATE = 'dms_value_info_max_tx_channel_rate'
    DMS_VALUE_INFO_MAX_RX_CHANNEL_RATE = 'dms_value_info_max_rx_channel_rate'
    DMS_VALUE_INFO_DATA_SERVICE_CAPABILITY = 'dms_value_info_data_service_capability'
    DMS_VALUE_INFO_SIM_CAPABILITY = 'dms_value_info_sim_capability'
    DMS_VALUE_INFO_RADIO_INTERFACE_LIST = 'dms_value_info_radio_interface_list'

class ListDynamicMetricsBackup:
    NAS_VALUE_SERVING_SYSTEM_REGISTRATION_STATE = 'nas_serv_sys_registration_state'
    NAS_VALUE_SERVING_SYSTEM_CS_ATTACH_STATE = 'nas_serv_sys_cs_attach_state'
    NAS_VALUE_SERVING_SYSTEM_PS_ATTACH_STATE = 'nas_serv_sys_ps_attach_state'
    NAS_VALUE_SERVING_SYSTEM_SELECTED_NETWORK = 'nas_serv_sys_selected_network'
    NAS_VALUE_SERVING_SYSTEM_RADIO_INTERFACES = 'nas_serv_sys_radio_interfaces'
    NAS_VALUE_ROAMING_INDICATOR = 'nas_roaming_indicator'
    NAS_VALUE_CURRENT_PLMN_MCC = 'nas_mcc'
    NAS_VALUE_CURRENT_PLMN_MNC = 'nas_mnc'
    NAS_VALUE_CURRENT_PLMN_DESCRIPTION = 'nas_plmn_description'
    NAS_VALUE_SIGNAL_STRENGTH_STRENGTH = 'nas_signal_strength'
    NAS_VALUE_SIGNAL_STRENGTH_RADIO_INTERFACE = 'nas_radio_interface'
    NAS_VALUE_IO = 'nas_io'
    NAS_VALUE_SINR = 'nas_sinr'
    NAS_VALUE_5G_SIGNAL_STRENGTH_RSRP = 'nas_rsrp'
    NAS_VALUE_5G_SIGNAL_STRENGTH_SNR = 'nas_snr'
    NAS_VALUE_5G_SIGNAL_STRENGTH_EXTENDED = 'nas_signal_strength_extended'
    NAS_VALUE_RX_CHAIN_0_INFO_RX_POWER = 'nas_rx_chain_0_rx_power'
    NAS_VALUE_RX_CHAIN_0_INFO_ECIO = 'nas_rx_chain_0_ecio'
    NAS_VALUE_RX_CHAIN_0_INFO_RSCP = 'nas_rx_chain_0_rscp'
    NAS_VALUE_RX_CHAIN_0_INFO_RSRP = 'nas_rx_chain_0_rsrp'
    NAS_VALUE_RX_CHAIN_0_INFO_PHASE = 'nas_rx_chain_0_phase'
    NAS_VALUE_RX_CHAIN_1_INFO_RX_POWER = 'nas_rx_chain_1_rx_power'
    NAS_VALUE_RX_CHAIN_1_INFO_ECIO = 'nas_rx_chain_1_ecio'
    NAS_VALUE_RX_CHAIN_1_INFO_RSCP = 'nas_rx_chain_1_rscp'
    NAS_VALUE_RX_CHAIN_1_INFO_RSRP = 'nas_rx_chain_1_rsrp'
    NAS_VALUE_RX_CHAIN_1_INFO_PHASE = 'nas_rx_chain_1_phase'
    NAS_VALUE_RX_CHAIN_2_INFO_RX_POWER = 'nas_rx_chain_2_rx_power'
    NAS_VALUE_RX_CHAIN_2_INFO_ECIO = 'nas_rx_chain_2_ecio'
    NAS_VALUE_RX_CHAIN_2_INFO_RSCP = 'nas_rx_chain_2_rscp'
    NAS_VALUE_RX_CHAIN_2_INFO_RSRP = 'nas_rx_chain_2_rsrp'
    NAS_VALUE_RX_CHAIN_2_INFO_PHASE = 'nas_rx_chain_2_phase'
    NAS_VALUE_RX_CHAIN_3_INFO_RX_POWER = 'nas_rx_chain_3_rx_power'
    NAS_VALUE_RX_CHAIN_3_INFO_ECIO = 'nas_rx_chain_3_ecio'
    NAS_VALUE_RX_CHAIN_3_INFO_RSCP = 'nas_rx_chain_3_rscp'
    NAS_VALUE_RX_CHAIN_3_INFO_RSRP = 'nas_rx_chain_3_rsrp'
    NAS_VALUE_RX_CHAIN_3_INFO_PHASE = 'nas_rx_chain_3_phase'
    NAS_VALUE_HOME_NETWORK_MCC = 'nas_home_network_mcc'
    NAS_VALUE_HOME_NETWORK_MNC = 'nas_home_network_mnc'
    NAS_VALUE_HOME_NETWORK_DESCRIPTION = 'nas_home_network_description'
    NAS_VALUE_NETWORK_NAME_SOURCE = 'nas_network_name_source'
    NAS_VALUE_NR5G_ARFCN = 'nas_nr5g_arfcn'
    NAS_VALUE_NR5G_CELL_INFORMATION_GLOBAL_CELL_ID = 'nas_cell_global_cell_id'
    NAS_VALUE_NR5G_CELL_INFORMATION_PHYSICAL_CELL_ID = 'nas_cell_physical_cell_id'
    NAS_VALUE_NR5G_CELL_INFORMATION_PLMN = 'nas_cell_plmn'
    NAS_VALUE_NR5G_CELL_INFORMATION_RSRP = 'nas_cell_rsrp'
    NAS_VALUE_NR5G_CELL_INFORMATION_RSRQ = 'nas_cell_rsrq'
    NAS_VALUE_NR5G_CELL_INFORMATION_SNR = 'nas_cell_snr'
    NAS_VALUE_NR5G_CELL_INFORMATION_TRACKING_AREA_CODE = 'nas_cell_tracking_area_code'
    NAS_LIST_RADIO_INTERFACE = 'nas_radio_interface'
    NAS_LIST_ACTIVE_BAND_CLASS = 'nas_active_band_class'
    NAS_LIST_ACTIVE_CHANNEL = 'nas_active_channel'
    NAS_EXTENDED_LIST_RADIO_INTERFACE = 'nas_extended_radio_interface'
    NAS_EXTENDED_LIST_ACTIVE_BAND_CLASS = 'nas_extended_active_band_class'
    NAS_EXTENDED_LIST_ACTIVE_CHANNEL = 'nas_extended_active_channel'
    NAS_BANDWIDTH_LIST_RADIO_INTERFACE = 'nas_bandwidth_radio_interface'
    NAS_BANDWIDTH_LIST_BANDWIDTH = 'nas_bandwidth_bandwidth'
    WDS_VALUE_TX_PACKETS_OK = 'wds_tx_packets_ok'
    WDS_VALUE_RX_PACKETS_OK = 'wds_rx_packets_ok'
    WDS_VALUE_TX_PACKETS_ERROR = 'wds_tx_packets_error'
    WDS_VALUE_RX_PACKETS_ERROR = 'wds_rx_packets_error'
    WDS_VALUE_TX_OVERFLOWS = 'wds_tx_overflows'
    WDS_VALUE_RX_OVERFLOWS = 'wds_rx_overflows'
    WDS_VALUE_TX_BYTES_OK = 'wds_tx_bytes_ok'
    WDS_VALUE_RX_BYTES_OK = 'wds_rx_bytes_ok'
    WDS_VALUE_TX_PACKETS_DROPPED = 'wds_tx_packets_dropped'
    WDS_VALUE_RX_PACKETS_DROPPED = 'wds_rx_packets_dropped'
    WDS_VALUE_CHANNEL_RATES_CHANNEL_TX_RATE_BPS = 'wds_channel_tx_rate_bps'
    WDS_VALUE_CHANNEL_RATES_CHANNEL_RX_RATE_BPS = 'wds_channel_rx_rate_bps'
    WDS_VALUE_CHANNEL_RATES_MAX_CHANNEL_TX_RATE_BPS = 'wds_max_channel_tx_rate_bps'
    WDS_VALUE_CHANNEL_RATES_MAX_CHANNEL_RX_RATE_BPS = 'wds_max_channel_rx_rate_bps'
    WDS_VALUE_CONNECTION_STATUS = 'wds_connection_status'

class ListDynamicMetrics:
    NAS_VALUE_SERVING_SYSTEM_REGISTRATION_STATE = 'nas_value_serving_system_registration_state'
    NAS_VALUE_SERVING_SYSTEM_CS_ATTACH_STATE = 'nas_value_serving_system_cs_attach_state'
    NAS_VALUE_SERVING_SYSTEM_PS_ATTACH_STATE = 'nas_value_serving_system_ps_attach_state'
    NAS_VALUE_SERVING_SYSTEM_SELECTED_NETWORK = 'nas_value_serving_system_selected_network'
    NAS_VALUE_SERVING_SYSTEM_RADIO_INTERFACES = 'nas_value_serving_system_radio_interfaces'
    NAS_VALUE_ROAMING_INDICATOR = 'nas_value_roaming_indicator'
    NAS_VALUE_CURRENT_PLMN_MCC = 'nas_value_current_plmn_mcc'
    NAS_VALUE_CURRENT_PLMN_MNC = 'nas_value_current_plmn_mnc'
    NAS_VALUE_CURRENT_PLMN_DESCRIPTION = 'nas_value_current_plmn_description'
    NAS_VALUE_SIGNAL_STRENGTH_STRENGTH = 'nas_value_signal_strength_strength'
    NAS_VALUE_SIGNAL_STRENGTH_RADIO_INTERFACE = 'nas_value_signal_strength_radio_interface'
    NAS_VALUE_IO = 'nas_value_io'
    NAS_VALUE_SINR = 'nas_value_sinr'
    NAS_VALUE_5G_SIGNAL_STRENGTH_RSRP = 'nas_value_5g_signal_strength_rsrp'
    NAS_VALUE_5G_SIGNAL_STRENGTH_SNR = 'nas_value_5g_signal_strength_snr'
    NAS_VALUE_5G_SIGNAL_STRENGTH_EXTENDED = 'nas_value_5g_signal_strength_extended'
    NAS_VALUE_RX_CHAIN_0_INFO_RX_POWER = 'nas_value_rx_chain_0_info_rx_power'
    NAS_VALUE_RX_CHAIN_0_INFO_ECIO = 'nas_value_rx_chain_0_info_ecio'
    NAS_VALUE_RX_CHAIN_0_INFO_RSCP = 'nas_value_rx_chain_0_info_rscp'
    NAS_VALUE_RX_CHAIN_0_INFO_RSRP = 'nas_value_rx_chain_0_info_rsrp'
    NAS_VALUE_RX_CHAIN_0_INFO_PHASE = 'nas_value_rx_chain_0_info_phase'
    NAS_VALUE_RX_CHAIN_1_INFO_RX_POWER = 'nas_value_rx_chain_1_info_rx_power'
    NAS_VALUE_RX_CHAIN_1_INFO_ECIO = 'nas_value_rx_chain_1_info_ecio'
    NAS_VALUE_RX_CHAIN_1_INFO_RSCP = 'nas_value_rx_chain_1_info_rscp'
    NAS_VALUE_RX_CHAIN_1_INFO_RSRP = 'nas_value_rx_chain_1_info_rsrp'
    NAS_VALUE_RX_CHAIN_1_INFO_PHASE = 'nas_value_rx_chain_1_info_phase'
    NAS_VALUE_RX_CHAIN_2_INFO_RX_POWER = 'nas_value_rx_chain_2_info_rx_power'
    NAS_VALUE_RX_CHAIN_2_INFO_ECIO = 'nas_value_rx_chain_2_info_ecio'
    NAS_VALUE_RX_CHAIN_2_INFO_RSCP = 'nas_value_rx_chain_2_info_rscp'
    NAS_VALUE_RX_CHAIN_2_INFO_RSRP = 'nas_value_rx_chain_2_info_rsrp'
    NAS_VALUE_RX_CHAIN_2_INFO_PHASE = 'nas_value_rx_chain_2_info_phase'
    NAS_VALUE_RX_CHAIN_3_INFO_RX_POWER = 'nas_value_rx_chain_3_info_rx_power'
    NAS_VALUE_RX_CHAIN_3_INFO_ECIO = 'nas_value_rx_chain_3_info_ecio'
    NAS_VALUE_RX_CHAIN_3_INFO_RSCP = 'nas_value_rx_chain_3_info_rscp'
    NAS_VALUE_RX_CHAIN_3_INFO_RSRP = 'nas_value_rx_chain_3_info_rsrp'
    NAS_VALUE_RX_CHAIN_3_INFO_PHASE = 'nas_value_rx_chain_3_info_phase'
    NAS_VALUE_HOME_NETWORK_MCC = 'nas_value_home_network_mcc'
    NAS_VALUE_HOME_NETWORK_MNC = 'nas_value_home_network_mnc'
    NAS_VALUE_HOME_NETWORK_DESCRIPTION = 'nas_value_home_network_description'
    NAS_VALUE_NETWORK_NAME_SOURCE = 'nas_value_network_name_source'
    NAS_VALUE_NR5G_ARFCN = 'nas_value_nr5g_arfcn'
    NAS_VALUE_NR5G_CELL_INFORMATION_GLOBAL_CELL_ID = 'nas_value_nr5g_cell_information_global_cell_id'
    NAS_VALUE_NR5G_CELL_INFORMATION_PHYSICAL_CELL_ID = 'nas_value_nr5g_cell_information_physical_cell_id'
    NAS_VALUE_NR5G_CELL_INFORMATION_PLMN = 'nas_value_nr5g_cell_information_plmn'
    NAS_VALUE_NR5G_CELL_INFORMATION_RSRP = 'nas_value_nr5g_cell_information_rsrp'
    NAS_VALUE_NR5G_CELL_INFORMATION_RSRQ = 'nas_value_nr5g_cell_information_rsrq'
    NAS_VALUE_NR5G_CELL_INFORMATION_SNR = 'nas_value_nr5g_cell_information_snr'
    NAS_VALUE_NR5G_CELL_INFORMATION_TRACKING_AREA_CODE = 'nas_value_nr5g_cell_information_tracking_area_code'
    NAS_LIST_RADIO_INTERFACE = 'nas_list_radio_interface'
    NAS_LIST_ACTIVE_BAND_CLASS = 'nas_list_active_band_class'
    NAS_LIST_ACTIVE_CHANNEL = 'nas_list_active_channel'
    NAS_EXTENDED_LIST_RADIO_INTERFACE = 'nas_extended_list_radio_interface'
    NAS_EXTENDED_LIST_ACTIVE_BAND_CLASS = 'nas_extended_list_active_band_class'
    NAS_EXTENDED_LIST_ACTIVE_CHANNEL = 'nas_extended_list_active_channel'
    NAS_BANDWIDTH_LIST_RADIO_INTERFACE = 'nas_bandwidth_list_radio_interface'
    NAS_BANDWIDTH_LIST_BANDWIDTH = 'nas_bandwidth_list_bandwidth'
    WDS_VALUE_TX_PACKETS_OK = 'wds_value_tx_packets_ok'
    WDS_VALUE_RX_PACKETS_OK = 'wds_value_rx_packets_ok'
    WDS_VALUE_TX_PACKETS_ERROR = 'wds_value_tx_packets_error'
    WDS_VALUE_RX_PACKETS_ERROR = 'wds_value_rx_packets_error'
    WDS_VALUE_TX_OVERFLOWS = 'wds_value_tx_overflows'
    WDS_VALUE_RX_OVERFLOWS = 'wds_value_rx_overflows'
    WDS_VALUE_TX_BYTES_OK = 'wds_value_tx_bytes_ok'
    WDS_VALUE_RX_BYTES_OK = 'wds_value_rx_bytes_ok'
    WDS_VALUE_TX_PACKETS_DROPPED = 'wds_value_tx_packets_dropped'
    WDS_VALUE_RX_PACKETS_DROPPED = 'wds_value_rx_packets_dropped'
    WDS_VALUE_CHANNEL_RATES_CHANNEL_TX_RATE_BPS = 'wds_value_channel_rates_channel_tx_rate_bps'
    WDS_VALUE_CHANNEL_RATES_CHANNEL_RX_RATE_BPS = 'wds_value_channel_rates_channel_rx_rate_bps'
    WDS_VALUE_CHANNEL_RATES_MAX_CHANNEL_TX_RATE_BPS = 'wds_value_channel_rates_max_channel_tx_rate_bps'
    WDS_VALUE_CHANNEL_RATES_MAX_CHANNEL_RX_RATE_BPS = 'wds_value_channel_rates_max_channel_rx_rate_bps'
    WDS_VALUE_CONNECTION_STATUS = 'wds_value_connection_status'
    GPSD_SKY_DATETIME = 'gpsd_sky_datetime'
    GPSD_SKY_TIMESTAMP = 'gpsd_sky_timestamp'
    GPSD_SKY_HDOP = 'gpsd_sky_hdop'
    GPSD_SKY_PDOP = 'gpsd_sky_pdop'
    GPSD_TPV_DATETIME = 'gpsd_tpv_datetime'
    GPSD_TPV_TIMESTAMP = 'gpsd_tpv_timestamp'
    GPSD_TPV_LAT = 'gpsd_tpv_lat'
    GPSD_TPV_LON = 'gpsd_tpv_lon'
    GPSD_TPV_ALT = 'gpsd_tpv_alt'
    GPSD_TPV_ALTHAE = 'gpsd_tpv_althae'
    GPSD_TPV_EPX = 'gpsd_tpv_epx'
    GPSD_TPV_EPY = 'gpsd_tpv_epy'
    GPSD_TPV_EPV = 'gpsd_tpv_epv'
    GPSD_TPV_SPEED = 'gpsd_tpv_speed'
    GPSD_TPV_EPS = 'gpsd_tpv_eps'
    THROUGHPUT_UPLOAD_KB = 'throughput_upload_kb'
    THROUGHPUT_DOWNLOAD_KB = 'throughput_download_kb'
    THROUGHPUT_UPLOAD_SPEED_KBPS = 'throughput_upload_speed_kbps'
    THROUGHPUT_DOWNLOAD_SPEED_KBPS = 'throughput_download_speed_kbps'
    

#
# [LoggingConfig]
#
# DEBUG > INFO > WARNING > ERROR > CRITICAL
class LoggingConfig():
    log_max_debug_level="DEBUG"
    log_debug_level_file="DEBUG"
    log_debug_level_console="DEBUG"
    log_file_path="qmi.log"
    log_dir_path="files"
    log_stats_file_path_json="stats.json"
    log_static_stats_file_path_csv="static_stats.csv"
    log_dynamic_stats_file_path_csv="dynamic_stats.csv"

#
# [InfoDevice]
#
# we only use this parameter to search for the device
# run `qmicli -d /dev/cdc-wdm0 --dms-set-operating-mode=reset` and check the logs with dmesg command
# fibocom usb (pc anthony) = (SN: 612EDE63 - IMEI: 869814060276197)
# quectel USB RM500Q (mocha4) = (SN: 5563df11 - IMEI: 863305040508037)
# quectel RM520N-GL (mocha4) = (SN: d50a6f2d - 
# quectel RM520N-GL (mocha3) = (SN: 3b30349e - IMEI: 868371050571278)
# quectel RM520N-GL (mocha2) = (SN: d964e22b - IMEI: 868371050575170)
# quectel RM520N-GL (mocha1) = (SN: 9f173352 - IMEI: 868371050574256)
# fibocom fm160-eau = (SN: d8631d27 - IMEI: 869814060967142)
# fibocom fm160-eau = (SN:7a04cd13 - IMEI: 869814060965070)

class InfoDevice():
    
    def __init__(self, serial_number, description, manufacturer, device_name, device_path):
        self.serial_number = serial_number
        self.description = description
        self.manufacturer = manufacturer
        self.device_name = device_name
        self.device_path = device_path

info_device_pc_usb_fibocom = None
info_device_pc_usb_quectel = None
info_device_mocha4 = None
info_device_mocha3 = None
info_device_mocha2 = None
info_device_mocha1 = None

# Slot 0
# serial_number = "612EDE63"
# description = "Fibocom Fibocom FM160 Modem_SN:612EDE63"
# manufacturer = "Fibocom"
# device_name = "Fibocom FM160 Modem_SN:612EDE63"
# device_path = "/dev/cdc-wdm2"
# info_device_pc_usb_fibocom = InfoDevice(serial_number, description, manufacturer, device_name, device_path)

# # Slot 1
serial_number = "5563df11"
description = "Quectel Wireless Solutions Co., Ltd. RM500Q-GL"
manufacturer = "Quectel"
device_name = "Quectel RM500Q-GL"
device_path = "/dev/cdc-wdm2"
info_device_pc_usb_quectel = InfoDevice(serial_number, description, manufacturer, device_name, device_path)

# Slot 2
# serial_number = "d50a6f2d"
# description = "Quectel Wireless Solutions Co., Ltd. RM520N-GL"
# manufacturer = "Quectel"
# device_name = "RM520N-GL"
# device_path = "/dev/cdc-wdm0"
# info_device_mocha4 = InfoDevice(serial_number, description, manufacturer, device_name, device_path)

# Slot 3
# serial_number = "3b30349e"
# description = "Quectel Wireless Solutions Co., Ltd. RM520N-GL"
# manufacturer = "Quectel"
# device_name = "RM520N-GL"
# device_path = "/dev/cdc-wdm0"
# info_device_mocha3 = InfoDevice(serial_number, description, manufacturer, device_name, device_path)

# Slot 4
# serial_number = "d964e22b"
# description = "Quectel Wireless Solutions Co., Ltd. RM520N-GL"
# manufacturer = "Quectel"
# device_name = "RM500Q-GL"
# device_path = "/dev/cdc-wdm0"
# info_device_mocha2 = InfoDevice(serial_number, description, manufacturer, device_name, device_path)

# Slot 5
# serial_number = "9f173352"
# description = "Quectel Wireless Solutions Co., Ltd. RM520N-GL"
# manufacturer = "Quectel"
# device_name = "RM500Q-GL"
# device_path = "/dev/cdc-wdm0"
# info_device_mocha1 = InfoDevice(serial_number, description, manufacturer, device_name, device_path)

info_device_list = [info_device_pc_usb_fibocom, info_device_pc_usb_quectel, info_device_mocha4, info_device_mocha3, info_device_mocha2, info_device_mocha1]

# [DataNetwork5GConfig]
class DataNetwork5GConfig:
    apn = "internet"
    ip_type = "4"

#
# [NetworkConfig]
#
class NetworkConfig():
    ip_broadcast_udp = "127.0.0.1"
    port_broadcast_udp = 5006
    ip_prometheus_collector = "PUBLIC_ADDRESS"
    # ip_prometheus_collector = "127.0.0.1"
    port_prometheus_collector = 5007

#
# [GeneralConfig]
#
class GeneralConfig():
    delay_seconds = 1
    get_location =  False
    get_throughput =  False
    connect_to_network = False
    exit_if_not_connect_to_network = False
    launch_udhcpc = True
    metric_dhcp = 0 # switching the value to 101 in debug mode
    reset_only_with_sn = True # using only serial number (when there are multiples devices with same name)


    
    # connect_to_network && exit_if_not_connect_to_network: if is not connected then it only save statics without WDS Data
    # connect_to_network && !exit_if_not_connect_to_network: if is not connected then it exit
    # !connect_to_network && any_value: if is not connected then collecting new data with errors