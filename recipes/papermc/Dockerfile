FROM eclipse-temurin:21

RUN mkdir /opt/app
RUN curl -o /opt/app/server.jar https://api.papermc.io/v2/projects/paper/versions/1.21.3/builds/82/downloads/paper-1.21.3-82.jar

WORKDIR /app
RUN java -jar /opt/app/server.jar --nogui

RUN echo eula=true > eula.txt

EXPOSE 25565

CMD ["java", "-Xms4G", "-Xmx4G", "-jar", "/opt/app/server.jar", "--nogui"]
