# dibao 服务。install 只管 dibao 自己；retire 才下线旧的 ai-home / ai-home-edits（com.charlie.menxia-approvals 不动）。
UID := $(shell id -u)
LABEL := com.charlie.dibao
LA := $(HOME)/Library/LaunchAgents
PLIST := $(LA)/$(LABEL).plist
OLD := com.charlie.ai-home com.charlie.ai-home-edits
STAMP := $(shell date +%Y%m%d)
RETIRED_DIR := $(LA)/_retired-dibao-$(STAMP)
PORT ?= 8766

.PHONY: install uninstall health test retire rollback status

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
	python3 -m unittest discover -s collectors -p 'test_*.py' -v

# 下线旧两个 launchd：先 install（bootstrap dibao 并 /health 通过），再 bootout 旧的，避免空窗。
# 注意：dibao 的 install 已含 health；若 dibao 已在跑，install 的 bootstrap 会报错，此时手动 `make health` 通过后直接跑 retire-only 步骤。
retire: install health
	mkdir -p $(RETIRED_DIR)
	for l in $(OLD); do \
	  launchctl bootout gui/$(UID)/$$l || true; \
	  [ -f $(LA)/$$l.plist ] && mv $(LA)/$$l.plist $(RETIRED_DIR)/ || true; \
	done

# 回滚：停 dibao，把最近一次 _retired-dibao-*/ 里的两个 plist 放回并 bootstrap。
rollback:
	-launchctl bootout gui/$(UID)/$(LABEL)
	d=$$(ls -d $(LA)/_retired-dibao-* | tail -1); \
	for l in $(OLD); do \
	  [ -f $$d/$$l.plist ] && mv $$d/$$l.plist $(LA)/ && launchctl bootstrap gui/$(UID) $(LA)/$$l.plist; \
	done

status:
	-launchctl list | grep com.charlie.ai-home
	-curl -fsS http://127.0.0.1:$(PORT)/health
