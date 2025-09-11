.PHONY: test
test: deps
	python3 src/katzen.py
deps:
	pyside6-rcc resources/resources.qrc -o src/resources_rc.py
	pyside6-uic mixchat.ui -o src/ui_mixchat.py
build: deps
	pyinstaller -F --noconsole -p src/ -p migrations/ --add-data=alembic.ini:alembic.ini src/katzen.py
	# pyinstaller katzen.spec
