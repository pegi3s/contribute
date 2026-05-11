This image can invoked using:

docker run -v /var/run/docker.sock:/var/run/docker.sock -v /contribute_history:/contribute_history -e USERID=$UID -e USER=$USER -e DISPLAY=$DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix -v $PWD:/data pegi3s/contribute
