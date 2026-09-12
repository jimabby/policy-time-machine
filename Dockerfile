FROM apache/airflow:3.1.0

# One source of truth for the dependency set. apache-airflow==3.1.0 is already
# satisfied by the base image, so pip treats that line as a no-op rather than
# resolving Airflow again.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV PYTHONPATH=/opt/airflow \
    PTM_INCLUDE_DIR=/opt/airflow/include \
    PTM_DB=/opt/airflow/include/ptm.db
