ARG EDA_BASE_IMAGE
FROM ${EDA_BASE_IMAGE}
USER 0
COPY tools/hailo_candidate.py /opt/rustberrypi/tools/hailo_candidate.py
COPY extensions/eda/plugins/event_source/hailo_release.py /opt/rustberrypi/extensions/eda/plugins/event_source/hailo_release.py
RUN mkdir -p /usr/share/ansible/collections/ansible_collections/rustberrypi/hailo/plugins/event_source \
    && ln -s /opt/rustberrypi/extensions/eda/plugins/event_source/hailo_release.py /usr/share/ansible/collections/ansible_collections/rustberrypi/hailo/plugins/event_source/hailo_release.py
USER 1001
