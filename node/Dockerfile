FROM node:20-alpine

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY agent.js ./
COPY plugins ./plugins

EXPOSE 3000

CMD ["npm", "start"]
