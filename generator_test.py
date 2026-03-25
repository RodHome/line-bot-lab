import json
import os
from datetime import datetime, timezone

def process_history_data(official_file, test_file, sort_key):
    print(f"🔄 開始處理五日累積名單: {test_file}")
    
    # 1. 讀取正式版今日產出的名單
    today_data = []
    if os.path.exists(official_file):
        with open(official_file, 'r', encoding='utf-8') as f:
            today_data = json.load(f)
            
    if not today_data:
        print(f"   ℹ️ 今日 {official_file} 無新標的入選。")
        
    # 2. 讀取歷史測試名單，建立去重字典
    history_dict = {}
    if os.path.exists(test_file):
        with open(test_file, 'r', encoding='utf-8') as f:
            old_data = json.load(f)
            if isinstance(old_data, list):
                for item in old_data:
                    code = item.get('code')
                    if code:
                        history_dict[code] = item
                        
    # 3. 執行更新與去重 (Upsert)
    today_date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    for item in today_data:
        code = item['code']
        # 提取官方名單上的日期，若無則用今天
        item_date = item.get('date', today_date_str)
        
        # 🔥 尋找歷史初次入榜日
        first_date = history_dict.get(code, {}).get('first_entry_date', item_date)
        
        # 🌟 【關鍵修改】尋找歷史初次入榜價格！
        # 如果舊字典裡有這個欄位，就嚴格保留；如果沒有（代表是全新進榜，或是舊資料還沒這欄位），就鎖定為「今天的價格」
        first_price = history_dict.get(code, {}).get('first_entry_price', item.get('price', 0.0))
        
        # 複製今日最新數據，並加上初次入榜的標籤與價格
        new_item = item.copy()
        new_item['first_entry_date'] = first_date
        new_item['first_entry_price'] = first_price # 👈 完美鎖定初始價格！
        
        # 覆寫進入字典
        history_dict[code] = new_item
        
    # 4. 長線交易日過濾法 (放大保留天數)
    # 抓出字典內所有的日期，去重並由新到舊排序，取前 30 個交易日 (約一個半月)
    all_dates = set(v.get('date') for v in history_dict.values() if v.get('date'))
    allowed_dates = sorted(list(all_dates), reverse=True)[:30] # 👈 從 5 改成 30
    
    # 只保留最後觸發日落在這 30 天內的標的
    final_list = [v for v in history_dict.values() if v.get('date') in allowed_dates]
    
    # 5. 排序並寫入檔案
    final_list.sort(key=lambda x: x.get(sort_key, 0), reverse=True)
    
    with open(test_file, 'w', encoding='utf-8') as f:
        json.dump(final_list, f, ensure_ascii=False, indent=4)
        
    print(f"   ✅ {test_file} 處理完成，歷史池共保留 {len(final_list)} 檔標的。")

if __name__ == "__main__":
    # 處理右側推薦 (使用 buy_value 排序)
    process_history_data(
        official_file='daily_recommendations.json', 
        test_file='daily_recommendations_test.json', 
        sort_key='buy_value'
    )
    
    # 處理左側黃金坑 (使用 score 排序)
    process_history_data(
        official_file='left_side_value.json', 
        test_file='left_side_value_test.json', 
        sort_key='score'
    )
