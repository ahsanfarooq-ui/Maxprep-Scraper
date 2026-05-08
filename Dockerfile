FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data

ENV DATA_DIR=/data
ENV STATE=TX
ENV SPORT=boys
ENV SEASON=2025-2026

CMD python texas_data_gap_finder.py --state $STATE --sport $SPORT --season $SEASON
