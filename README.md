# Overview
This project has been created to monitoring 5G modems using qmi commands with libqmi library. It has following features:

- Connect to the 5G data network
- Get device metrics
- Dinamyc device path parameter
- Save metrics into csv file and json file
- Send metrics to module prometheus server
- Send metrics to broadcast on localhost

# Installation

## Prerequisites

First, install the system prerequisites

`sudo apt install -y gir1.2-qmi-1.0 python3-pandas python3-prometheus-client`

or

`sudo apt update -y && sudo apt install -y gir1.2-qmi-1.0 python3.11-venv libgirepository1.0-dev libcairo2-dev && sudo apt autoremove`

## Install

Download project and install the specific requirements of this project

`git clone git@ants-gitlab.inf.um.es:5ginfra/logging-qmi.git`

`cd logging-qmi`

`source env/bin/activate`

`pip install -r requirements.txt`

To exit virtual enviroment mode:

`deactivate`

# Configuration

There are 2 files to customize the execution of this script:

1. `modules/config_data.py` file

In `config_data.py` file is posssible customizing global and constant variables, modify some data structs when you will need add new features, customize some aspects related to your device (serial number, name, usb path), customize logging config, data network config or general config.

You should customize `serial_number` parameter by serial number of your device. This parameter is used by the script to reboot the device if necessary. Also, you need to customize IP Adresses.

Para realizar esto, basta con dejar descomentado solo el objeto InfoDevice que hace referencia al modem que se va a utilizar. El resto de InfoDevice deben de estar comentados. A excepción de la declaración inicial con valor None la cual se debe de mantener.

# Execution

## Basic usage
To execute this script you need sudo permissions. You can run

`sudo env/bin/python3.11 logging_qmi.py -d /dev/cdc-wdm0`

`CTRL + C`

## Service mode

`sudo cp logging-qmi.service /etc/systemd/system/logging-qmi.service`

`sudo systemctl enable --now logging-qmi.service`

`sudo systemctl status logging-qmi`

`sudo systemctl [start|stop|restart|status] logging-qmi`

View logs

`systemctl status logging-qmi`

`tail -f files/modem_[IMEI]/[NAME_FILE_OF_DEVICE]_qmi.log`

# Troubleshouting

En caso de que haya problema con el reinicio del modem se puede realizar un reinicio manual con el script `reset_usb.py`.

> `python3 reset_usb.py search NAME_DEVICE`

> `python3 reset_usb.py search Quectel`

> `python3 reset_usb.py search Fibocom`

En caso de que se conecte a otra red, lo más probable es que no se haya definido previamente y de forma manual el PLMN al que se debería conectar 

> `qmicli -d /dev/cdc-wdm0 --nas-set-system-selection-preference=5gnr,manual=99910`

Si esto no funciona, también es posible que no haya configurado y establecido un perfil por defecto al que contectarse.

> `qmicli -d /dev/cdc-wdm0 --wds-create-profile=3gpp,apn=internet,pdp-type=IP,disabled=no,no-roaming=no`

> `qmicli -d /dev/cdc-wdm0 --wds-set-default-profile-number=3gpp,NUMEROPROFILE`

> `qmicli -d /dev/cdc-wdm0 --wds-get-profile-list=3gpp`

> `qmicli -d /dev/cdc-wdm0 --wds-get-default-profile-number=3gpp`

Si nada de esto funciona, lo más probable es que no haya buena cobertura o que haya un problema en la red. Para ello, ponte en contacto con el administrador de la red 5G.

