FROM pegi3s/docker

ENV DEBIAN_FRONTEND=noninteractive

# Install only the libraries required for Firefox to run
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      git \
      build-essential \
      autoconf \
      automake \
      libtool \
      ca-certificates \
      wget \
      tar \
      xz-utils \
      libgtk-3-0 \
      libdbus-glib-1-2 \
      libx11-xcb1 \
      libasound2t64 \
      fonts-liberation \
      libgbm1 \
      libcanberra-gtk3-module \
    && rm -rf /var/lib/apt/lists/*

# Download and extract Firefox
RUN wget -O /tmp/firefox.tar.xz \
      "https://download.mozilla.org/?product=firefox-latest&os=linux64" && \
    tar -xJf /tmp/firefox.tar.xz -C /opt && \
    ln -sf /opt/firefox/firefox /usr/local/bin/firefox && \
    rm -f /tmp/firefox.tar.xz

# Basic configuration to skip first-run screens
RUN mkdir -p /opt/firefox/defaults/pref && \
    echo 'pref("general.config.filename", "mozilla.cfg");' > /opt/firefox/defaults/pref/local-settings.js && \
    echo 'pref("general.config.obscure_value", 0);' >> /opt/firefox/defaults/pref/local-settings.js && \
    printf 'lockPref("browser.aboutwelcome.enabled", false);\nlockPref("datareporting.policy.dataSubmissionEnabled", false);' > /opt/firefox/mozilla.cfg

ENV DISPLAY=:0

# Install streamlit

RUN apt update && apt install -y python3-pip
RUN pip install streamlit --break-system-packages
RUN pip install streamlit-scroll-to-top --break-system-packages
RUN mkdir -p ~/.streamlit/
RUN echo "[browser]\ngatherUsageStats = false\n" > ~/.streamlit/config.toml
RUN echo "[server]\nheadless = true\n" > ~/.streamlit/config.toml

# Copy Python script

COPY interface.py /opt
RUN echo "#!/bin/bash" > /opt/start
RUN echo "firefox http://localhost:8501 & exec streamlit run interface.py" >> /opt/start
RUN chmod 777 /opt/start
WORKDIR /opt

# Clone the project

RUN apt install -y git 

#RUN git clone https://github.com/pegi3s/dockerfiles.git #########################

CMD ["/opt/start"]
 

# docker run -v /var/run/docker.sock:/var/run/docker.sock   -v /contribute_history:/contribute_history -e USERID=$UID   -e USER=$USER   -e DISPLAY=$DISPLAY   -v /tmp/.X11-unix:/tmp/.X11-unix   -v $PWD:/data pegi3s/contribute