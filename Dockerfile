FROM apache/airflow:3.1.0

COPY requirements.txt /tmp/requirements.txt
# Airflow pins its own deps; --no-deps on airflow itself is handled by the
# constraint that the base image already satisfies apache-airflow==3.1.0.
RUN pip install --no-cache-dir \
      "apache-airflow-providers-standard>=1.10.0" \
      "apache-airflow-providers-common-ai>=0.8.0" \
      "pydantic>=2.7" "PyYAML>=6.0"

ENV PYTHONPATH=/opt/airflow \
    PTM_INCLUDE_DIR=/opt/airflow/include \
    PTM_DB=/opt/airflow/include/ptm.db
