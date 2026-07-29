# Container image rather than a zip: trafilatura, psycopg2 and the Anthropic SDK
# together exceed Lambda's 250 MB unzipped limit.
FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY alerts/     ${LAMBDA_TASK_ROOT}/alerts/
COPY analysis/   ${LAMBDA_TASK_ROOT}/analysis/
COPY collectors/ ${LAMBDA_TASK_ROOT}/collectors/
COPY db/         ${LAMBDA_TASK_ROOT}/db/
COPY market/     ${LAMBDA_TASK_ROOT}/market/
COPY utils/      ${LAMBDA_TASK_ROOT}/utils/
COPY config.py main.py lambda_handler.py ${LAMBDA_TASK_ROOT}/

# Config comes from Lambda environment variables, not a bundled .env -- the
# file is gitignored and must never be baked into an image.
ENV LOG_FILE=""

CMD ["lambda_handler.handler"]
