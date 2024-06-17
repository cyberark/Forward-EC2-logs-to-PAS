To create the zip file for the lambda run the following commands:

# Create a container matching the lambda runtime version:
docker run -v $(pwd):/lambda -it --entrypoint /bin/bash public.ecr.aws/lambda/python:3.12

# Create python virtual environment
python3.12 -m venv venv
source venv/bin/activate

# Create the Lambda function directory structure
mkdir lambda_function
cd lambda_function

# Copy the source code into the mounted directory, including the python file and the requirements, then run install the dependencies
pip install -r requirements.txt -t .

# Zip the content
zip -r <LAMBDA_NAME>.zip .

# Deactivate the virtual environemnt
deactivate
