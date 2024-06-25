import os
import subprocess
import re

# udhcpc is normally started from busybox's ifup, with compiled-in parameters:
#     udhcpc -R -n -p /var/run/udhcpc.wlan0.pid -i wlan0
# If wlan0 does not associate on boot, udhcpc tries to get a lease, fails, and
# exits (because of the -n parameter).
#
# Busybox udhcpc:
#
# udhcpc [-Cfbnqtvo] [-c CID] [-V VCLS] [-H HOSTNAME] [-i INTERFACE]
#        [-p pidfile] [-r IP] [-s script] [-O dhcp-option] ...
#
# -V,--vendorclass=CLASSID    Vendor class identifier
# -i,--interface=INTERFACE    Interface to use (default eth0)
# -H,-h,--hostname=HOSTNAME   Client hostname
# -c,--clientid=CLIENTID      Client identifier
# -C,--clientid-none          Suppress default client identifier
# -p,--pidfile=file           Create pidfile
# -r,--request=IP             IP address to request
# -s,--script=file            Run file at DHCP events
#                             (default /usr/share/udhcpc/default.script)
# -t,--retries=N              Send up to N request packets
# -T,--timeout=N              Try to get a lease for N seconds (default 3)
# -A,--tryagain=N             Wait N seconds (default 20) after failure
# -O,--request-option=OPT     Request DHCP option OPT (cumulative)
# -o,--no-default-options     Do not request any options
#                             (unless -O is also given)
# -f,--foreground             Run in foreground
# -b,--background             Background if lease is not immediately obtained
# -S,--syslog                 Log to syslog too
# -n,--now                    Exit with failure if lease is not immediately
#                             obtained
# -q,--quit                   Quit after obtaining lease
# -R,--release                Release IP on quit
# -a,--arping                 Use arping to validate offered address


def start(ifname, hostname=None, retries=5):
    cmd = ["udhcpc", "-R", "-n",
           "-p", "/var/run/udhcpc." + ifname + ".pid",
           "-i", ifname]

    # cmd = ["udhcpc", "-q", "-f",
    #        "-i", ifname]
    if hostname is not None:
        cmd.extend(["-x", "hostname:%s" % hostname])
    cmd.extend(["-T", "1",
                "-t", str(retries)])
    try:
        subprocess.check_output(cmd)
    except:
        return False
    
    return True

def update_metric(ifname, metric=0):

    try:
        # Comando para eliminar la ruta por defecto
        del_default_route_cmd = ["ip", "route", "del", "default", "dev", ifname]

        # Ejecutar el comando para eliminar la ruta por defecto
        subprocess.run(del_default_route_cmd, check=True)

        # Comando para agregar la nueva ruta con la métrica deseada
        add_default_route_cmd = ["ip", "route", "add", "default", "dev", ifname, "metric", str(metric)]

        # Ejecutar el comando para agregar la nueva ruta con la métrica
        subprocess.run(add_default_route_cmd, check=True)
    except:
        return False
    
    return True

def stop(ifname):
    pid_file = "/var/run/udhcpc." + ifname + ".pid"

    # Check if pid file exists
    if not os.path.exists(pid_file):
        return False

    try:
        with open(pid_file, "r") as file:
            pid = file.read().strip()
            subprocess.call(["kill", pid])
            #os.remove(pid_file)
    except:
        return False

    return True

def pid(ifname):
    try:
        f = open("/var/run/udhcpc." + ifname + ".pid")
        pid = int(f.read())
        f.close()
        return pid
    except:
        return None


def get_lease(pid):
    try:
        subprocess.check_output(["kill", "-USR1", pid])
        return True
    except:
        return False

def pre_config(ifname):

    cmd = ["ip", "link", "set", ifname, "down"]

    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    except Exception as e:
        raise Exception(e.output.decode().rstrip())
    
    cmd = ["echo", "Y"]
    f = open(f"/sys/class/net/{ifname}/qmi/raw_ip", mode="w")
   
    try:
        subprocess.run(cmd, stdout=f)
    except Exception as e:
        raise Exception(str(e))
    

    cmd = ["ip", "link", "set", ifname, "up"]

    try:
        subprocess.check_output(cmd)
    except Exception as e:
        raise Exception(e.output.decode().rstrip())
    
    cmd = ["ip", "link", "set", "qlen", "16", "dev", ifname]

    try:
        subprocess.check_output(cmd)
    except Exception as e:
        raise Exception(e.output.decode().rstrip())

    return

# if __name__ == "__main__":
#     print(pre_config("wwan0"))