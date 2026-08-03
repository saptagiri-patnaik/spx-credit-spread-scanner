# Container image rather than a zip: trafilatura, psycopg2 and the Anthropic SDK
# together exceed Lambda's 250 MB unzipped limit.
FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt ${LAMBDA_TASK_ROOT}/

# Installed as three layers rather than one, because Docker Desktop's built-in
# proxy (192.168.65.1:3128, unavoidable on Windows and not configurable) drops a
# single blob this large mid-upload. Every other layer uploaded fine while this
# one failed on every attempt, through BuildKit's retries and the classic pusher
# alike, always with "broken pipe" at the same point. Smaller blobs get through,
# and the build gets finer cache granularity as a side benefit.
#
# The names are listed here but the versions are NOT: requirements.txt stays the
# single source of truth, and these lines only decide which packages land in
# which layer. A name that stops matching costs a slower push, never a wrong
# install -- the final line installs everything regardless.
# Split finer on 3 Aug. Three layers was not enough: the two that remained came
# out at 64.7 MB and 67.5 MB, and the proxy killed both on every attempt --
# eleven pushes across two deploy runs and a direct `docker push`, always the
# same two digests, always "broken pipe" or "use of closed network connection".
# Everything smaller went through untouched on the same runs, which is the whole
# diagnosis: it is blob size, not the registry, the credentials or the layer.
#
# lxml, babel and regex are trafilatura's heavyweight transitive dependencies and
# are installed by name first purely to move them into their own blob. They are
# unpinned on purpose -- the trafilatura line immediately below resolves the
# actual constraint, and the final line still installs everything, so a name that
# stops matching costs a slower push and never a wrong install.
RUN pip install --no-cache-dir lxml babel regex
RUN grep -iE '^(trafilatura)' ${LAMBDA_TASK_ROOT}/requirements.txt > /tmp/a.txt \
    && pip install --no-cache-dir -r /tmp/a.txt
RUN grep -iE '^(psycopg2-binary)' ${LAMBDA_TASK_ROOT}/requirements.txt > /tmp/b.txt \
    && pip install --no-cache-dir -r /tmp/b.txt
RUN grep -iE '^(SQLAlchemy)' ${LAMBDA_TASK_ROOT}/requirements.txt > /tmp/c.txt \
    && pip install --no-cache-dir -r /tmp/c.txt
RUN grep -iE '^(anthropic)' ${LAMBDA_TASK_ROOT}/requirements.txt > /tmp/d.txt \
    && pip install --no-cache-dir -r /tmp/d.txt
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

# Declared last on purpose: an ARG invalidates every layer after it, so putting
# the version here keeps the pip install cached across builds. .git is excluded
# from the build context, so the value has to be computed by deploy.ps1 and passed
# in rather than read from the tree.
ARG APP_VERSION=unknown
ENV APP_VERSION=$APP_VERSION

CMD ["lambda_handler.handler"]
