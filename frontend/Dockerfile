# Образ kaspisa-web: Next.js standalone.
FROM node:24-slim AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# Адрес api попадает в rewrites при сборке; в рантайме переменная ничего не меняет.
ENV KASPISA_API_ORIGIN=http://api:8000
RUN npm run build

FROM node:24-slim
WORKDIR /app
# Docker ставит HOSTNAME равным id контейнера: без переопределения сервер слушает не тот адрес.
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
COPY --from=build --chown=node:node /app/public ./public
USER node
EXPOSE 3000
CMD ["node", "server.js"]
