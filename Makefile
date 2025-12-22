.PHONY: test
test: deps
	python3 src/katzenqt/katzen.py
deps:
	pyside6-rcc resources/resources.qrc -o src/katzenqt/resources_rc.py
	pyside6-uic ui/mixchat.ui -o src/katzenqt/ui_mixchat.py
	pyside6-uic ui/font-settings.ui -o src/katzenqt/ui_font_settings.py
build: deps
	pyinstaller -F --noconsole -p src/ -p migrations/ --add-data=alembic.ini:alembic.ini src/katzenqt/katzen.py
	# pyinstaller katzen.spec
