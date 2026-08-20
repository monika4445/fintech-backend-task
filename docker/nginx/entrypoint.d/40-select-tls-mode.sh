#!/bin/sh
# Выбирает режим по наличию сертификата. Отрабатывает после штатного шага
# envsubst (20-envsubst-on-templates.sh), порядок задан номером в имени файла.
#
# Зачем режим HTTP-only. Сертификата при первом запуске ещё нет, nginx с
# ssl_certificate на несуществующий файл не стартует, а без работающего nginx
# certbot не пройдёт webroot-проверку. Классический тупик первого запуска, из-за
# которого смена DOMAIN даёт простой вместо переключения.
set -eu

CONF_DIR=/etc/nginx/conf.d

# Штатный конфиг образа тоже слушает 80 и мешается под ногами.
rm -f "$CONF_DIR/default.conf"

# Если шаблон переименовали, а этот скрипт не поправили, нужно упасть громко.
# Молчаливое продолжение оставило бы nginx в наполовину собранной конфигурации.
for required in 05-shared.conf 10-http-redirect.conf 10-http-direct.conf 20-ssl.conf \
                proxy_params.inc; do
    if [ ! -f "$CONF_DIR/$required" ]; then
        echo "$0: ОШИБКА: $CONF_DIR/$required не найден после envsubst" >&2
        exit 1
    fi
done

if [ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]; then
    echo "$0: сертификат для ${DOMAIN} найден, включаем HTTPS"
    rm -f "$CONF_DIR/10-http-direct.conf"
else
    echo "$0: сертификата для ${DOMAIN} нет, поднимаемся в режиме HTTP-only"
    rm -f "$CONF_DIR/10-http-redirect.conf"
    rm -f "$CONF_DIR/20-ssl.conf"
fi
