.PHONY: test
test:
	pyside6-rcc resources/resources.qrc -o src/resources_rc.py && \
	pyside6-uic mixchat.ui -o src/ui_mixchat.py && python3 src/katzen.py
