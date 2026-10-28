# dibao 服务（A 期·核心）。install 只管 dibao 自己，不碰旧的 ai-home / ai-home-edits（下线归 WP-09b 的 retire）。
UID := $(shell id -u)
LABEL := com.charlie.dibao
PLIST := $(HOME)/Library/LaunchAgents/$(LABEL).plist
PORT ?= 8766

.PHONY: install uninstall health test

install:
	mkdir -p $(HOME)/.config/dibao $(HOME)/Library/Logs/dibao
	cp -n service/config.example.toml $(HOME)/.config/dibao/config.toml || true
	cp launchd/$(LABEL).plist $(PLIST)
	launchctl bootstrap gui/$(UID) $(PLIST)
	sleep 3
	$(MAKE) health

uninstall:
	-launchctl bootout gui/$(UID)/$(LABEL)
	rm -f $(PLIST)

health:
	curl -fsS http://127.0.0.1:$(PORT)/health

test:
	python3 -m unittest discover -s service/tests -v
