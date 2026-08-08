"""券商閘道層（Broker Gateway）。

以抽象介面隔離 Shioaji SDK，讓策略層完全不依賴券商實作：

* :mod:`~microtx.broker.base` —— ``BrokerGateway`` 抽象基底類別。
* :mod:`~microtx.broker.shioaji_gateway` —— 真實 Shioaji 實作（模擬 / 實盤）。
* :mod:`~microtx.broker.paper_gateway` —— 純本地模擬，無需帳號即可跑單元測試與 Demo。
"""
