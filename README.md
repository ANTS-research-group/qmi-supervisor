# Overview
This project has been created to monitor and control 5G modems using qmi commands with libqmi library. 

This development has been tailored to be used on the University of Murcia ANTS research group 5G network testbed  [(GAIALAB)](https://ants.inf.um.es/en/gaialab) described in [this paper](https://doi.org/10.1007/978-3-031-20936-9_29)
It can be used as a general purpose modem controller and telemetry collection software.

It implements the following features:
- Controls Qualcomm based modems using libQMI (Qualcomm MSM Interface)
- It can manage more than a single modem
- Connects the modem to a 5G data network (PDN)
- Get real time device status
- GNSS positioning and data tagging using GPSd
- Dynamic device path management
- Store metrics into csv file and json file
- Send real time metrics to a prometheus server listener (QMI-Supervisor-Prometheus-Target)
- Send metrics using UDP broadcast on localhost to enable custom integrations

If you are using this development in your integration / testbed / application, we'd love to hear about it and you can cite us using Zenodos's DOI: [![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.12528538.svg)](https://doi.org/10.5281/zenodo.12528538) and the general testbed description and architecture is described in [this paper](https://doi.org/10.1007/978-3-031-20936-9_29)

```
    QMI-Supervisor - a 5G QMI modem controller
    Copyright (C) 2024 Anthony Joel Pogo Medina - Universidad de Murcia

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.
```

# Installation

## Prerequisites

First, install the system prerequisites

`sudo apt install -y gir1.2-qmi-1.0 python3-pandas python3-prometheus-client`

or

`sudo apt update -y && sudo apt install -y gir1.2-qmi-1.0 python3.11-venv libgirepository1.0-dev libcairo2-dev && sudo apt autoremove`

## Install

Download project and install the specific requirements of this project

`git clone https://github.com/ANTS-research-group/qmi-supervisor.git`

`cd qmi-supervisor`

`source env/bin/activate`

`pip install -r requirements.txt`

To exit virtual enviroment mode:

`deactivate`

# Configuration

There are 2 files to customize the execution of this script:

1. `modules/config_data.py` file

In `config_data.py` file you can customize global and constant variables, modify some data structs when you will need add new features, customize some aspects related to your device (serial number, name, usb path), to customize logs, PDN settins or general settings.

You should customize `serial_number` parameter with the serial number of your device. This parameter is used by the script to reboot the device if necessary. Also, you need to customize telemetry IP Adresses.

# Execution

## Basic usage
To execute this script you need root permissions. You can run

`sudo env/bin/python3.11 logging_qmi.py -d /dev/cdc-wdm0`

## Service mode

`sudo cp qmi-supervisor.service /etc/systemd/system/qmi-supervisor.service`

`sudo systemctl enable --now qmi-supervisor.service`

`sudo systemctl status qmi-supervisor`

View logs

`systemctl status qmi-supervisor`

`tail -f files/modem_[IMEI]/[NAME_FILE_OF_DEVICE]_qmi.log`

# Troubleshouting

You can manually reset the modem with `reset_usb.py` if there is any unexpected behaviour.

> `python3 reset_usb.py search NAME_DEVICE`

> `python3 reset_usb.py search Quectel`

> `python3 reset_usb.py search Fibocom`

If the modem connects to an unexpected network, you should configure the modem with qmicli. 

> `qmicli -d /dev/cdc-wdm0 --nas-set-system-selection-preference=5gnr,manual=99910`

You can also add a profile to the modem so it can connect automatically to the defined network.

> `qmicli -d /dev/cdc-wdm0 --wds-create-profile=3gpp,apn=internet,pdp-type=IP,disabled=no,no-roaming=no`

> `qmicli -d /dev/cdc-wdm0 --wds-set-default-profile-number=3gpp,[GIVEN-PROFILE-NUMBER]`

> `qmicli -d /dev/cdc-wdm0 --wds-get-profile-list=3gpp`

> `qmicli -d /dev/cdc-wdm0 --wds-get-default-profile-number=3gpp`

If the modem still fails to attach to the network check coverage and other issues, such as proper SIM configuration and provisioning.

