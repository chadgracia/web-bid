import json


def lambda_handler(event, context):
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html"},
        "body": "<h1>web-bid is live</h1><p>Deploy pipe works.</p>",
    }
