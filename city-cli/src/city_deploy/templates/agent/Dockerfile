FROM city-sdk
WORKDIR /opt/agent
COPY card.yaml city.yaml ./
COPY agent ./agent
ENV CITY_CARD=/opt/agent/card.yaml
ENV CITY_CITY=/opt/agent/city.yaml
ENV PYTHONPATH=/opt/agent
USER city
ENTRYPOINT ["python", "-m", "city_sdk"]
