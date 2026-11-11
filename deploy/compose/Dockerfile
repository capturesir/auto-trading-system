FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC \
    PATH="/srv/trading/.venv/bin:${PATH}"

RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod 755 /usr/sbin/policy-rc.d \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
        sudo \
        gosu \
        passwd \
        util-linux \
        chrony \
        rsync \
        openssh-client \
    && rm -rf /var/lib/apt/lists/* /usr/sbin/policy-rc.d

WORKDIR /srv/trading
COPY . /srv/trading

RUN python -m venv /srv/trading/.venv \
    && /srv/trading/.venv/bin/pip install --upgrade pip \
    && /srv/trading/.venv/bin/pip install -e ".[ibkr,universe]" \
    && install -m 0755 /srv/trading/deploy/compose/systemctl-host.sh /usr/bin/systemctl \
    && printf '%s\n' 'trader ALL=(root) NOPASSWD: /usr/bin/systemctl restart ibgateway.service' \
        > /etc/sudoers.d/trader-ibgateway \
    && chmod 0440 /etc/sudoers.d/trader-ibgateway \
    && visudo -cf /etc/sudoers.d/trader-ibgateway \
    && chmod 0755 /srv/trading/deploy/compose/entrypoint.sh \
        /srv/trading/deploy/compose/timer-loop.sh \
        /srv/trading/scripts/run_paper_intents_daily.sh \
        /srv/trading/scripts/pool_rebuild.sh

ENTRYPOINT ["/srv/trading/deploy/compose/entrypoint.sh"]
