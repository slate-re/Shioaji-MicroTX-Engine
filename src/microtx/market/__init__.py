"""行情層。

負責訂閱管理、``simtrade`` 試撮過濾、Tick 正規化，
並以事件佇列將行情推給策略層，確保 Shioaji callback 執行緒不被阻塞。
"""
