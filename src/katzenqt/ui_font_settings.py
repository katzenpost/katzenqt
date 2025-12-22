# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'font-settings.ui'
##
## Created by: Qt User Interface Compiler version 6.9.3
##
## WARNING! All changes made in this file will be lost when recompiling UI file!
################################################################################

from PySide6.QtCore import (QCoreApplication, QDate, QDateTime, QLocale,
    QMetaObject, QObject, QPoint, QRect,
    QSize, QTime, QUrl, Qt)
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QCursor,
    QFont, QFontDatabase, QGradient, QIcon,
    QImage, QKeySequence, QLinearGradient, QPainter,
    QPalette, QPixmap, QRadialGradient, QTransform)
from PySide6.QtWidgets import (QAbstractButton, QApplication, QDialog, QDialogButtonBox,
    QFormLayout, QLabel, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget)

class Ui_FontSettingsDialog(object):
    def setupUi(self, FontSettingsDialog):
        if not FontSettingsDialog.objectName():
            FontSettingsDialog.setObjectName(u"FontSettingsDialog")
        FontSettingsDialog.resize(266, 175)
        sizePolicy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.MinimumExpanding)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(FontSettingsDialog.sizePolicy().hasHeightForWidth())
        FontSettingsDialog.setSizePolicy(sizePolicy)
        self.formLayout_2 = QFormLayout(FontSettingsDialog)
        self.formLayout_2.setObjectName(u"formLayout_2")
        self.verticalLayout = QVBoxLayout()
        self.verticalLayout.setObjectName(u"verticalLayout")
        self.verticalLayout.setContentsMargins(15, 15, 15, 15)
        self.label = QLabel(FontSettingsDialog)
        self.label.setObjectName(u"label")

        self.verticalLayout.addWidget(self.label)

        self.formLayout = QFormLayout()
        self.formLayout.setObjectName(u"formLayout")
        self.formLayout.setLabelAlignment(Qt.AlignmentFlag.AlignCenter)
        self.formLayout.setFormAlignment(Qt.AlignmentFlag.AlignHCenter|Qt.AlignmentFlag.AlignTop)
        self.formLayout.setContentsMargins(-1, -1, 10, -1)
        self.contactName_label = QLabel(FontSettingsDialog)
        self.contactName_label.setObjectName(u"contactName_label")

        self.formLayout.setWidget(0, QFormLayout.ItemRole.LabelRole, self.contactName_label)

        self.contactName_toolButton = QToolButton(FontSettingsDialog)
        self.contactName_toolButton.setObjectName(u"contactName_toolButton")

        self.formLayout.setWidget(0, QFormLayout.ItemRole.FieldRole, self.contactName_toolButton)

        self.messageText_label = QLabel(FontSettingsDialog)
        self.messageText_label.setObjectName(u"messageText_label")

        self.formLayout.setWidget(1, QFormLayout.ItemRole.LabelRole, self.messageText_label)

        self.messageText_toolButton = QToolButton(FontSettingsDialog)
        self.messageText_toolButton.setObjectName(u"messageText_toolButton")

        self.formLayout.setWidget(1, QFormLayout.ItemRole.FieldRole, self.messageText_toolButton)

        self.everythingElse_label = QLabel(FontSettingsDialog)
        self.everythingElse_label.setObjectName(u"everythingElse_label")

        self.formLayout.setWidget(2, QFormLayout.ItemRole.LabelRole, self.everythingElse_label)

        self.everythingElse_toolButton = QToolButton(FontSettingsDialog)
        self.everythingElse_toolButton.setObjectName(u"everythingElse_toolButton")

        self.formLayout.setWidget(2, QFormLayout.ItemRole.FieldRole, self.everythingElse_toolButton)


        self.verticalLayout.addLayout(self.formLayout)

        self.buttonBox = QDialogButtonBox(FontSettingsDialog)
        self.buttonBox.setObjectName(u"buttonBox")
        self.buttonBox.setOrientation(Qt.Orientation.Horizontal)
        self.buttonBox.setStandardButtons(QDialogButtonBox.StandardButton.Cancel|QDialogButtonBox.StandardButton.Ok)

        self.verticalLayout.addWidget(self.buttonBox)


        self.formLayout_2.setLayout(0, QFormLayout.ItemRole.LabelRole, self.verticalLayout)


        self.retranslateUi(FontSettingsDialog)
        self.buttonBox.accepted.connect(FontSettingsDialog.accept)
        self.buttonBox.rejected.connect(FontSettingsDialog.reject)

        QMetaObject.connectSlotsByName(FontSettingsDialog)
    # setupUi

    def retranslateUi(self, FontSettingsDialog):
        FontSettingsDialog.setWindowTitle(QCoreApplication.translate("FontSettingsDialog", u"Dialog", None))
        self.label.setText(QCoreApplication.translate("FontSettingsDialog", u"<html><head/><body><p><span style=\" font-weight:700;\">Set preferred display font</span></p></body></html>", None))
        self.contactName_label.setText(QCoreApplication.translate("FontSettingsDialog", u"Contact name", None))
        self.contactName_toolButton.setText(QCoreApplication.translate("FontSettingsDialog", u"Change font...", None))
        self.messageText_label.setText(QCoreApplication.translate("FontSettingsDialog", u"Message text", None))
        self.messageText_toolButton.setText(QCoreApplication.translate("FontSettingsDialog", u"Change font...", None))
        self.everythingElse_label.setText(QCoreApplication.translate("FontSettingsDialog", u"Everything else", None))
        self.everythingElse_toolButton.setText(QCoreApplication.translate("FontSettingsDialog", u"Change font...", None))
    # retranslateUi

