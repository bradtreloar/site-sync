#!/usr/bin/python3

from dotenv import dotenv_values
from io import StringIO
import os
from paramiko import AutoAddPolicy, SSHClient
import re
import requests
from requests.sessions import Session
from scp import SCPClient
import shutil
import urllib3
import yaml

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
requests.packages.urllib3.util.ssl_.DEFAULT_CIPHERS = 'ALL:@SECLEVEL=1'


class RemoteClient:

    def __init__(self, config):
        self.config = {
            "port": 22,
            "key_filename": None,
        }
        self.config.update(config)

    def get_scp_client(self):
        ssh_client = self.get_ssh_client()
        scp_client = SCPClient(ssh_client.get_transport())
        return scp_client

    def get_ssh_client(self):
        ssh_client = SSHClient()
        ssh_client.set_missing_host_key_policy(AutoAddPolicy())
        ssh_client.connect(self.config["host"],
                           username=self.config["user"],
                           port=self.config["port"],
                           key_filename=self.config["key_filename"])
        return ssh_client

    def exec_command(self, command):
        ssh_client = self.get_ssh_client()
        stdin, stdout, stderr = ssh_client.exec_command(command)
        error = stderr.read().decode('utf8')
        stdin.close()
        ssh_client.close()
        if error:
            raise RemoteCommandError(error)
        return stdout.read().decode('utf8').strip()

    def download_file(self, src, dest):
        scp_client = self.get_scp_client()
        scp_client.get(src, dest, recursive=True)

    def start_webauth_session(self):
        try:
            config = self.config["webauth"]
        except KeyError:
            raise NoWebauthConfigException
        session = Session()
        response = session.post(config["login_url"], {
            "login": "login",
            "username": config["username"],
            "password": config["password"],
        }, params={
            "target": "",
            "auth_id": "",
            "ap_name": "",
        }, verify=False)
        if response.status_code != 200:
            raise LoginError
        response = session.post(config["webauth_url"], {
            "rs": "is_lsys_image_exist",
            "rsargs[]": "root",
            "csrf_token": "",
        }, verify=False)
        if response.status_code != 200:
            raise WebauthError


class DrupalClient:
    IGNORED_FILES = ["php", "css", "js", "styles", "simpletest"]

    def __init__(self, ssh_config):
        self.remote_client = RemoteClient(ssh_config)

    def exists(self):
        return exists(self.remote_client, "drupal")

    def version(self):
        status = self.remote_client.exec_command(
            "cd drupal && vendor/bin/drupal site:status")
        return status.split('\n')[1].strip().split(" ")[-1]

    def site_names(self):
        sites_filepath = "drupal/web/sites/sites.php"
        if not exists(self.remote_client, sites_filepath):
            return ['default']
        lines = self.remote_client.exec_command(
            "cat " + sites_filepath).split('\n')
        site_names = []
        for line in lines:
            pattern = r"""\$sites\[['"]{1}[^'"]{1,}['"]{1}\]\s{0,1}=\s{0,1}['"]{1}([^'"]{1,})['"]{1};"""
            matches = re.search(pattern, line.strip())
            if matches:
                site_name = matches[1]
                if site_name not in site_names:
                    site_names.append(site_name)
        return site_names

    def sites_settings(self):
        sites_settings = {}
        for site_name in self.site_names():
            sites_settings[site_name] = self.site_settings(site_name)
        return sites_settings

    def site_settings(self, site_name):
        dotenv = self.remote_client.exec_command("cat drupal/.env")
        settings = dotenv_values(stream=StringIO(dotenv))
        prefix = site_name.upper().replace(".", "_")
        return {
            "database": {
                "host": settings.get(prefix + "_DBHOST", "localhost"),
                "port": settings.get(prefix + "_DBPORT", "3306"),
                "database": settings[prefix + "_DBNAME"],
                "username": settings[prefix + "_DBUSER"],
                "password": settings[prefix + "_DBPASS"],
            }
        }

    def export_database(self, site_name, site_settings):
        filepath = "drupal/data/{}/drupal.sql".format(site_name)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        home_path = self.remote_client.exec_command("pwd")
        self.remote_client.exec_command(
            "mkdir -p {}/tmp".format(home_path))
        temporary_database_filepath = "{}/tmp/drupal_{}.sql".format(
            home_path, site_name)
        database_settings = site_settings["database"]
        command = "MYSQL_PWD='{password}' mysqldump --user='{username}' '{database}' > {file}".format(
            database=database_settings["database"],
            username=database_settings["username"],
            password=database_settings["password"],
            file=temporary_database_filepath
        )
        self.remote_client.exec_command(command)
        self.remote_client.download_file(temporary_database_filepath, filepath)
        self.remote_client.exec_command(
            "rm {}".format(temporary_database_filepath))

    def download_site_files(self, site_name):
        files_path = "web/sites/{}/files".format(site_name)
        # Delete and then remake files directory.
        local_dir = os.path.join("drupal", files_path)
        shutil.rmtree(local_dir)
        os.makedirs(local_dir, exist_ok=True)
        for filename in ls(self.remote_client, "drupal/" + files_path):
            if filename not in DrupalClient.IGNORED_FILES:
                remote_path = "drupal/" + files_path + "/" + filename
                local_path = os.path.join(local_dir, filename)
                self.remote_client.download_file(remote_path, local_path)

    def start_webauth_session(self):
        self.remote_client.start_webauth_session()


class WordpressClient:
    DATABASE_FILEPATH = "wordpress/data/wordpress.sql"

    def __init__(self, ssh_config):
        self.remote_client = RemoteClient(ssh_config)

    def exists(self):
        return exists(self.remote_client, "wordpress")

    def version(self):
        return self.remote_client.exec_command(
            "cd wordpress && vendor/bin/wp core version")

    def site_settings(self):
        dotenv = self.remote_client.exec_command("cat wordpress/.env")
        settings = dotenv_values(stream=StringIO(dotenv))
        return {
            "database": {
                "host": settings.get("DB_HOST", "localhost"),
                "port": settings.get("DB_PORT", "3306"),
                "database": settings["DB_NAME"],
                "username": settings["DB_USER"],
                "password": settings["DB_PASSWORD"],
            }
        }

    def export_database(self):
        database_filepath = "wordpress/data/wordpress.sql"
        os.makedirs(os.path.dirname(
            database_filepath), exist_ok=True)
        home_path = self.remote_client.exec_command("pwd")
        self.remote_client.exec_command(
            "mkdir -p {}/tmp".format(home_path))
        temporary_database_filepath = home_path + "/tmp/wordpress.sql"
        site_settings = self.site_settings()
        database_settings = site_settings["database"]
        command = "MYSQL_PWD='{password}' mysqldump --user='{username}' '{database}' > {file}".format(
            database=database_settings["database"],
            username=database_settings["username"],
            password=database_settings["password"],
            file=temporary_database_filepath
        )
        self.remote_client.exec_command(command)
        self.remote_client.download_file(
            temporary_database_filepath, database_filepath)
        self.remote_client.exec_command(
            "rm {}".format(temporary_database_filepath))

    def download_site_files(self):
        uploads_path = "web/app/uploads"
        # Delete and then remake files directory.
        local_dir = os.path.join("wordpress", uploads_path)
        shutil.rmtree(local_dir)
        os.makedirs(local_dir, exist_ok=True)
        for filename in ls(self.remote_client, "wordpress/" + uploads_path):
            if filename != "cache":
                remote_path = "wordpress/" + uploads_path + "/" + filename
                local_path = os.path.join("wordpress", uploads_path, filename)
                self.remote_client.download_file(remote_path, local_path)

    def start_webauth_session(self):
        self.remote_client.start_webauth_session()


class RemoteCommandError(BaseException):
    pass


class LoginError(BaseException):
    pass


class WebauthError(BaseException):
    pass


class NoWebauthConfigException(BaseException):
    pass


CLIENTS = {
    "drupal": DrupalClient,
    "wordpress": WordpressClient,
}


def main():
    config = load_config("site.yml")
    app = config["app"]
    ssh_config = config["ssh"]
    client = CLIENTS[app](ssh_config)
    try:
        client.start_webauth_session()
    except NoWebauthConfigException:
        pass
    if app == "drupal":
        sites_settings = client.sites_settings()
        for site_name, site_settings in sites_settings.items():
            client.export_database(site_name, site_settings)
            client.download_site_files(site_name)
    elif app == "wordpress":
        client.export_database()
        client.download_site_files()


def load_config(config_filepath):
    with open(config_filepath) as file:
        return yaml.safe_load(file)


def exists(client, path):
    try:
        client.exec_command("stat {}".format(path))
    except RemoteCommandError:
        return False
    return True


def ls(client, dirpath):
    return client.exec_command(
        "ls {}".format(dirpath)).split("\n")


if __name__ == "__main__":
    main()
