FROM locustio/locust:2.47.0
RUN pip install --no-cache-dir prometheus-client==0.26.0
COPY jfcore.py locustfile.py /home/locust/
WORKDIR /home/locust
EXPOSE 8089 9646
CMD ["-f", "/home/locust/locustfile.py", "--class-picker"]
