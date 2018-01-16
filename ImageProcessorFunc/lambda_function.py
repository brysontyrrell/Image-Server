import base64
import json
import logging
import mimetypes
import os
import tempfile

import boto3
from botocore.exceptions import ClientError

from PIL import Image, ImageOps

root = logging.getLogger()
if root.handlers:
    for handler in root.handlers:
        root.removeHandler(handler)

logging.basicConfig(
    datefmt='%Y-%m-%d %H:%M:%S',
    format='%(asctime)s - %(levelname)s - %(name)s - '
           '%(module)s:%(funcName)s:%(lineno)s - %(message)s',
    level=logging.INFO)

s3_bucket = boto3.resource('s3').Bucket(os.environ['S3_BUCKET'])

tempdir = tempfile.mkdtemp()
logging.info('Temporary directory: %s', tempdir)


def response(body, status_code, filename=''):
    logging.info('Body: %s', body)

    if isinstance(body, dict):
        resp = {
            'isBase64Encoded': False,
            'statusCode': str(status_code),
            'body': json.dumps(body),
            'headers': {'Content-Type': 'application/json'}
        }
    else:
        mimetype = mimetypes.guess_type(filename)[0]
        logging.info('Mimetype: %s', mimetype)

        with open(body, 'rb') as fobj:
            body_content = base64.b64encode(fobj.read()).decode('ascii')

        logging.info('Body content: %s', body_content)
        resp = {
            'isBase64Encoded': True,
            'statusCode': str(status_code),
            'body': body_content,
            'headers': {'Content-Type': mimetype}
        }

    logging.info("Response: Base64: %s, Code: %s, Body '%s',Headers: %s",
                 resp["isBase64Encoded"], resp["statusCode"],
                 resp["body"][:20] + '...', resp["headers"])

    return resp


def file_name_ext(filename, size):
    file_, extension = os.path.splitext(filename)
    return f'{file_}-{size}{extension}' if size > 0 else filename, \
           extension[1:].upper()


def retrieve_original_image(filename):
    path = os.path.join(tempdir, filename)

    logging.info("Downloading '%s' from S3", filename)
    try:
        s3_bucket.download_file(filename, path)
    except ClientError:
        logging.error("File '%s' not found", filename)
        return {'errorMessage': f'Not Found: {filename}'}, 404

    logging.info("Downloaded file '%s' at size %s", path, os.stat(path).st_size)

    return path, 200


def resize_image(input_path, output_filename, output_path, size):
    """
    :param str input_path:
    :param str output_filename:
    :param str output_path:
    :param int size:

    :return:
    :rtype: io.BytesIo
    """
    with Image.open(input_path) as image:
        if image.width == image.height:
            width, height = size, size

        elif image.width > image.height:
            width = size
            height = int((float(size) / float(image.width)) * image.height)

        else:
            width = int((float(size) / float(image.height)) * image.width)
            height = size

        logging.info("Resizing image '%s' from %sx%s to %sx%s",
                     input_path, image.width, image.height, width, height)
        resized_image = image.resize((width, height), Image.LANCZOS)

        logging.info('Saving resized image to: %s', output_path)
        resized_image.save(output_path, format=image.format)

    logging.info("Created image file '%s' at size %s", output_path,
                 os.stat(output_path).st_size)

    try:
        logging.info("Uploading '%s' to S3", output_path)
        s3_bucket.upload_file(output_path, output_filename)

    except ClientError:
        logging.exception("Error uploading '%s' to S3", output_path)
        return {'errorMessage': 'Internal Server Error: Unable to '
                                'upload resized image to S3 bucket'}, 500

    return output_path, 201


def retrieve_resized_image(filename, size):
    resized_filename, extension = file_name_ext(filename, size)

    path = os.path.join(tempdir, resized_filename)
    logging.info('Output image file: %s', path)

    logging.info("Downloading '%s' from S3", resized_filename)
    try:
        s3_bucket.download_file(resized_filename, path)

    except ClientError:
        logging.warning("Could not find '%s' on S3; downloading original...",
                        resized_filename)
        resp, code = retrieve_original_image(filename)

        if isinstance(resp, dict):
            return resp, code
        else:
            return resize_image(resp, resized_filename, path, size)

    logging.info("Downloaded file '%s' at size %s", path, os.stat(path).st_size)
    return path, 200


def retrieve_image(filename, size):
    if size == 0:
        return response(*retrieve_original_image(filename), filename=filename)
    else:
        return response(
            *retrieve_resized_image(filename, size), filename=filename)


def lambda_handler(event, context):
    logging.debug('Lambda Event: %s', event)

    if event['queryStringParameters']:
        try:
            img_size = int(event['queryStringParameters'].get('size', 0))
        except ValueError:
            return response(
                {'errorMessage': 'Bad Request: Invalid size value'}, 400)

        if img_size < 0:
            return response(
                {'errorMessage': 'Bad Request: Invalid size value:'}, 400)
    else:
        img_size = 0

    img_name = event['path'].split('/')[-1]

    if not img_name:
        return response(
            {'errorMessage': 'Bad Request: No image filename provided'}, 400)

    logging.info('Image filename: %s', img_name)
    logging.info('Requested size: %s', img_size)

    return retrieve_image(img_name, img_size)
