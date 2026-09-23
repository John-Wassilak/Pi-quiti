FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY piquiti ./piquiti
RUN pip install --no-cache-dir .
ENTRYPOINT ["piquiti"]
CMD ["run"]
