#!/bin/sh
# Отрабатывает после штатного шага envsubst (20-envsubst-on-templates.sh),
# порядок задан номером в имени файла.
#
# Без этого шага смена DOMAIN даёт простой, а не переключение: сертификата на
# новый домен ещё нет, nginx с ssl_certificate на несуществующий файл не
# стартует, а без работающего nginx certbot не пройдёт webroot-проверку.
# Классический тупик первого запуска.
set -eu

# Штатный конфиг образа тоже слушает 80 и мешается под ногами.
rm -f /etc/nginx/conf.d/default.conf

if [ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]; then
    echo "$0: сертификат для ${DOMAIN} найден, включаем HTTPS"
    exit 0
fi

echo "$0: сертификата для ${DOMAIN} нет, поднимаемся в режиме HTTP-only"
rm -f /etc/nginx/conf.d/20-ssl.conf

# Редирект на https в этом режиме увёл бы клиента в никуда, поэтому на время
# отсутствия сертификата тот же location проксирует напрямую.
sed -i \
    -e 's|^        return 301 https://\$host\$request_uri;|        include /etc/nginx/conf.d/proxy_params.inc;|' \
    /etc/nginx/conf.d/10-http.conf
