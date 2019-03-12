import requests
import urllib3
import uuid
import cfnresponse
import time
import boto3
import json
from dynamo_lock import LockerClient

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_HEADER = {"content-type": "application/json"}

#git patch
def lambda_handler(event, context):

    try:
        physicalResourceId = str(uuid.uuid4())
        if 'PhysicalResourceId' in event:
            physicalResourceId = event['PhysicalResourceId']
        # only deleting the vault_pass from parameter store
        if event['RequestType'] == 'Delete':
            if not delete_password_from_param_store():
                return cfnresponse.send(event, context, cfnresponse.FAILED,
                                        "Failed to delete 'PTA_Vault_Password' from parameter store, see detailed error in logs", {}, physicalResourceId)
            delete_sessions_table()
            return cfnresponse.send(event, context, cfnresponse.SUCCESS, None, {}, physicalResourceId)

        if event['RequestType'] == 'Create':

            requestUsername = event['ResourceProperties']['Username']
            requestPvwaIp = event['ResourceProperties']['PVWAIP']
            requestPassword = event['ResourceProperties']['Password']
            requestKeyPairSafe = event['ResourceProperties']['KeyPairSafe']
            requestKeyPairName = event['ResourceProperties']['KeyPairName']
            requestAWSRegionName = event['ResourceProperties']['AWSRegionName']
            requestAWSAccountId = event['ResourceProperties']['AWSAccountId']
            requestPtaIp = event['ResourceProperties']['PTAIP']

            isPasswordSaved = save_password_to_param_store(requestPassword)
            if not isPasswordSaved:  # if password failed to be saved
                return cfnresponse.send(event, context, cfnresponse.FAILED, "Failed to create Vault user's password in Parameter Store",
                                        {}, physicalResourceId)

            pvwaSessionId = logon_pvwa(requestUsername, requestPassword, requestPvwaIp)
            if not pvwaSessionId:
                return cfnresponse.send(event, context, cfnresponse.FAILED, "Failed to connect to PVWA, see detailed error in logs",
                                        {}, physicalResourceId)

            if not create_session_table():
                return cfnresponse.send(event, context, cfnresponse.FAILED,
                                        "Failed to create 'PTASessions' table in DynamoDB, see detailed error in logs",
                                        {}, physicalResourceId)

            #  Creating KeyPair Safe
            isSafeCreated = create_safe(requestKeyPairSafe, "", requestPvwaIp, pvwaSessionId)
            if not isSafeCreated:
                return cfnresponse.send(event, context, cfnresponse.FAILED,
                                        "Failed to create the Key Pairs safe: {0}, see detailed error in logs".format(requestKeyPairSafe),
                                        {}, physicalResourceId)

            #  key pair is optional parameter
            if not requestKeyPairName:
                print("Key Pair name parameter is empty, the solution will not create a new Key Pair")
                return cfnresponse.send(event, context, cfnresponse.SUCCESS, None, {}, physicalResourceId)
            else:
                awsKeypair = create_new_key_pair_on_AWS(requestKeyPairName)

                if awsKeypair is False:
                    # Account already exist, no need to create it, can't insert it to the vault
                    return cfnresponse.send(event, context, cfnresponse.FAILED, "Failed to create Key Pair '{0}' in AWS".format(requestKeyPairName),
                                            {}, physicalResourceId)
                if awsKeypair is True:
                    return cfnresponse.send(event, context, cfnresponse.FAILED, "Key Pair '{0}' already exists in AWS".format(requestKeyPairName),
                                            {}, physicalResourceId)
                # Create the key pair account on KeyPairs vault
                isAwsAccountCreated = create_key_pair_in_vault(pvwaSessionId, requestKeyPairName, awsKeypair, requestPvwaIp,
                                                              requestKeyPairSafe, requestAWSAccountId, requestAWSRegionName)
                if not isAwsAccountCreated:
                    return cfnresponse.send(event, context, cfnresponse.FAILED,
                                            "Failed to create Key Pair {0} in safe {1}. see detailed error in logs".format(requestKeyPairName, requestKeyPairSafe),
                                            {}, physicalResourceId)

                return cfnresponse.send(event, context, cfnresponse.SUCCESS, None, {}, physicalResourceId)

    except Exception as e:
        print("Exception occurred:{0}:".format(e))
        return cfnresponse.send(event, context, cfnresponse.FAILED, "Exception occurred: {0}".format(e), {})

    finally:
        if 'pvwaSessionId' in locals():  # pvwaSessionId has been declared
            if pvwaSessionId:  # Logging off the session in case of successful logon
                logoff_pvwa(requestPvwaIp, pvwaSessionId)


# Creating a safe, if a failure occur, retry 3 time, wait 10 sec. between retries
def create_safe(safeName, cpmName, pvwaIP, sessionId, numberOfDaysRetention=7):
    header = DEFAULT_HEADER
    header.update({"Authorization": sessionId})
    createSafeUrl = "https://{0}/PasswordVault/WebServices/PIMServices.svc/Safes".format(pvwaIP)
    # Create new safe, default number of days retention is 7, unless specified otherwise
    data = """
                {{
          "safe":{{
        "SafeName":"{0}",
        "Description":"",
        "OLACEnabled":false,
        "ManagingCPM":"{1}",
        "NumberOfDaysRetention":"{2}"
          }}
        }}
    """.format(safeName, cpmName, numberOfDaysRetention)

    for i in range(0, 3):
        createSafeRestResponse = call_rest_api_post(createSafeUrl, data, header)

        if createSafeRestResponse.status_code == requests.codes.conflict:
            print("The Safe '{0}' already exists".format(safeName))
            return True
        elif createSafeRestResponse.status_code == requests.codes.bad_request:
            print("Failed to create safe '{0}', error 400: bad request".format(safeName))
            return False
        elif createSafeRestResponse.status_code == requests.codes.created:  # safe created
            print("The Safe '{0}' was successfully created".format(safeName))
            return True
        else:  # Error creating safe, retry for 3 times, with 10 seconds between retries
            print("Error creating safe, status code:{0}, will retry in 10 seconds".format(createSafeRestResponse.status_code))
            if i == 3:
                print("Failed to create safe after several retries, status code:{0}"
                      .format(createSafeRestResponse.status_code))
                return False
        time.sleep(10)


def logon_pvwa(username, password, pvwaUrl):
    print('Start Logon to PVWA REST API')
    logonUrl = 'https://{0}/PasswordVault/API/auth/Cyberark/Logon'.format(pvwaUrl)
    restLogonData = """{{
        "username": "{0}",
        "password": "{1}",
        }}""".format(username, password)
    restResponse = call_rest_api_post(logonUrl, restLogonData, DEFAULT_HEADER)
    if not restResponse:
        return None
    if restResponse.status_code == requests.codes.ok:
        jsonParsedResponse = restResponse.json()
        print("The logon completed successfully")
        return jsonParsedResponse
    elif restResponse.status_code == requests.codes.not_found:
        print("Logon to PVWA failed, error 404: page not found")
        return None
    elif restResponse.status_code == requests.codes.forbidden:
        print("Logon to PVWA failed, authentication failure for user {0}".format(username))
        return None
    else:
        print("Logon to PVWA failed, status code:{0}".format(restResponse.status_code))
        return None


def logoff_pvwa(pvwaUrl, connectionSessionToken):
    print('Start Logoff to PVWA REST API')
    header = DEFAULT_HEADER
    header.update({"Authorization": connectionSessionToken})
    logoffUrl = 'https://{0}/PasswordVault/API/auth/Logoff'.format(pvwaUrl)
    restLogoffData = ""
    try:
        restResponse = call_rest_api_post(logoffUrl, restLogoffData, DEFAULT_HEADER)
    except Exception:
        # if couldn't logoff, nothing to do, return
        return

    if(restResponse.status_code == requests.codes.ok):
        jsonParsedResponse = restResponse.json()
        print("session logged off successfully")
        return True
    else:
        print("Logoff failed")
        return False


def call_rest_api_post(url, request, header):
    try:
        restResponse = requests.post(url, data=request, timeout=30, verify=False, headers=header)
    except Exception as e:
        print("Error occurred during POST request to PVWA. Exception: {0}".format(e))
        return None
    return restResponse

# Search if Key pair exist, if not - create it, return the pem key, False for error
def create_new_key_pair_on_AWS(keyPairName):
    ec2Client = boto3.client('ec2')

    # throws exception if key not found, if exception is InvalidKeyPair.Duplicate return True
    try:

        keyPairResponse = ec2Client.create_key_pair(
            KeyName=keyPairName,
            DryRun=False
        )
    except Exception as e:
        if e.response["Error"]["Code"] == "InvalidKeyPair.Duplicate":
            print("Key Pair '{0}' already exists".format(keyPairName))
            return True
        else:
            print("Creating new Key Pair failed. error code: {0}".format(e.response["Error"]["Code"]))
            return False

    return keyPairResponse["KeyMaterial"]


def create_key_pair_in_vault(session, awsKeyName, privateKeyValue, pvwaIP, safeName, awsAccountId, awsRegionName):
    header = DEFAULT_HEADER
    header.update({"Authorization": session})

    trimmedPEMKey = str(privateKeyValue).replace("\n", "\\n")
    trimmedPEMKey = trimmedPEMKey.replace("\r", "\\r")

    # AWS.<AWS Account>.<Region name>.<key pair name>
    uniqueUsername = "AWS.{0}.{1}.{2}".format(awsAccountId, awsRegionName, awsKeyName)
    print("Creating account with username:{0}".format(uniqueUsername))
    url = "https://{0}/PasswordVault/api/Accounts".format(pvwaIP)
    data = """{{
        "safeName":"{0}",
        "platformId":"{1}",
        "address":"1.1.1.1",
        "secretType": "key",
        "secret": "{2}",
        "userName":"{3}"
    }}""".format(safeName, "UnixSSHKeys", trimmedPEMKey, uniqueUsername)
    restResponse = call_rest_api_post(url, data, header)

    if restResponse.status_code == requests.codes.created:
        print("Key Pair created successfully in safe '{0}'".format(safeName))
        return True
    elif restResponse.status_code == requests.codes.conflict:
        print("Key Pair created already exists in safe {0}".format(safeName))
        return True
    else:
        print("Failed to create Key Pair in safe '{0}', status code:{1}".format(safeName, restResponse.status_code))
        return False

def create_session_table():
    try:
        sessionsTableLock = LockerClient('PTASessions')
        sessionsTableLock.create_lock_table()
    except Exception as e:
        print("Failed to create 'PTASessions' table in DynamoDB. Exception: {0}".format(e))
        return None

    print("Table 'PTASessions' created successfully")
    return True


def save_password_to_param_store(password):
    try:
        ssmClient = boto3.client('ssm')
        ssmClient.put_parameter(
            Name="PTA_Vault_Password",
            Description="Vault Password",
            Value=password,
            Type="SecureString"
        )
    except Exception as e:
        print("Unable to create parameter 'PTA_Vault_Password' in Parameter Store. Exception: {0}".format(e))
        return False
    return True


def delete_password_from_param_store():
    try:
        ssmClient = boto3.client('ssm')
        ssmClient.delete_parameter(
            Name='PTA_Vault_Password'
        )
        print("Parameter 'PTA_Vault_Password' deleted successfully from Parameter Store")
        return True
    except Exception as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return True
        else:
            print("Failed to delete parameter 'PTA_Vault_Password' from Parameter Store. Error code: {0}".format(e.response["Error"]["Code"]))
            return False


def delete_sessions_table():
    try:
        dynamodb = boto3.resource('dynamodb')
        sessionsTable = dynamodb.Table('PTASessions')
        sessionsTable.delete()
        return
    except Exception:
        print("Failed to delete 'PTASessions' table from DynamoDB")
        return
