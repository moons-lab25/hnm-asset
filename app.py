import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, date
import os
import FinanceDataReader as fdr
from streamlit_gsheets import GSheetsConnection 

# --- 설정 및 컬럼 정의 ---
ASSET_COLUMNS = ["Record_Date", "Owner", "Category", "Sub_Category", "Liquidity", "Amount", "Profit", "Note"]
SIM_COLUMNS = ["Sim_Date", "Target_Age", "Monthly_Investment", "Result_Final_Asset"]
PORTFOLIO_COLUMNS = ["Owner", "Broker", "Account_Type", "Ticker", "Stock_Name", "Liquidity", "Shares", "Avg_Price", "LastUpdated"] 
REALIZED_COLUMNS = ["Date", "Owner", "Category", "Item", "Amount", "Note"]

EOK = 100_000_000

# --- 공통 유틸 ---
def _ensure_columns(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    return df[cols]

def safe_to_numeric(df: pd.DataFrame, col: str) -> pd.DataFrame:
    if df.empty or col not in df.columns:
        return df
    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df

def normalize_record_date(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "Record_Date" not in df.columns:
        return df
    df["Record_Date"] = df["Record_Date"].astype(str).str.strip()
    df["Record_DT"] = pd.to_datetime(df["Record_Date"], format='mixed', errors="coerce")
    df["Record_Month"] = df["Record_DT"].dt.to_period("M").astype(str)
    return df

# --- 환율 및 시세 캐싱 ---
@st.cache_data(ttl=3600)
def get_usd_krw_rate() -> float:
    try:
        df = fdr.DataReader('USD/KRW')
        if not df.empty:
            return float(df.iloc[-1]['Close'])
    except Exception:
        pass
    return 1350.0 

@st.cache_data(ttl=3600)
def get_current_price(ticker: str) -> float:
    ticker = str(ticker).strip().upper()
    if ticker == "CASH" or not ticker or ticker == "NAN":
        return 1.0 
    try:
        df = fdr.DataReader(ticker)
        if not df.empty:
            price = float(df.iloc[-1]['Close'])
            is_korean_stock = len(ticker) == 6 and ticker[0].isdigit()
            if not is_korean_stock:
                usd_krw = get_usd_krw_rate()
                price = price * usd_krw
            return price
    except Exception:
        pass
    return 0.0

# --- 구글 시트 연동 I/O 함수 ---
def get_gsheets_conn():
    return st.connection("gsheets", type=GSheetsConnection)

def load_history() -> pd.DataFrame:
    conn = get_gsheets_conn()
    try:
        df = conn.read(worksheet="AssetHistory", ttl=0).dropna(how="all")
    except Exception:
        df = pd.DataFrame(columns=ASSET_COLUMNS)
        
    df = _ensure_columns(df, ASSET_COLUMNS)
    df = safe_to_numeric(df, "Amount")
    df = safe_to_numeric(df, "Profit")
    
    if not df.empty:
        # [에러 방어] 결측치(NaN, <NA>)를 빈 문자열로 안전하게 치환
        for c in ["Owner", "Category", "Sub_Category", "Liquidity", "Note"]:
            df[c] = df[c].fillna('').astype(str).replace({'<NA>': '', 'nan': '', 'None': ''})
        
        df['Category'] = df['Category'].replace("금융자산", "금융자산(수기)")

        def auto_fill_liquidity(row):
            if pd.isna(row['Liquidity']) or str(row['Liquidity']).strip() == "":
                non_liquid_keywords = ['연금', 'IRP', '퇴직']
                if any(kw in str(row['Sub_Category']) for kw in non_liquid_keywords):
                    return "비유동"
                return "유동"
            return str(row['Liquidity']).strip()
        
        df['Liquidity'] = df.apply(auto_fill_liquidity, axis=1)
        df = normalize_record_date(df)
    return df

def save_history(df: pd.DataFrame):
    conn = get_gsheets_conn()
    df = _ensure_columns(df, ASSET_COLUMNS)
    for c in ["Record_DT", "Record_Month"]:
        if c in df.columns:
            df = df.drop(columns=[c])
    conn.update(worksheet="AssetHistory", data=df)

def load_realized() -> pd.DataFrame:
    conn = get_gsheets_conn()
    try:
        df = conn.read(worksheet="Realized", ttl=0).dropna(how="all")
    except Exception:
        df = pd.DataFrame(columns=REALIZED_COLUMNS)
    
    df = _ensure_columns(df, REALIZED_COLUMNS)
    # [에러 방어] 결측치 치환
    for c in ["Date", "Owner", "Category", "Item", "Note"]:
        df[c] = df[c].fillna('').astype(str).replace({'<NA>': '', 'nan': '', 'None': ''})
    df = safe_to_numeric(df, "Amount")
    return df

def save_realized(df: pd.DataFrame):
    conn = get_gsheets_conn()
    df = _ensure_columns(df, REALIZED_COLUMNS)
    conn.update(worksheet="Realized", data=df)

def save_portfolio(df: pd.DataFrame):
    conn = get_gsheets_conn()
    df = _ensure_columns(df, PORTFOLIO_COLUMNS)
    conn.update(worksheet="Portfolio", data=df)

def get_live_portfolio() -> pd.DataFrame:
    conn = get_gsheets_conn()
    try:
        port_df = conn.read(worksheet="Portfolio", ttl=0).dropna(how="all")
    except Exception:
        port_df = pd.DataFrame(columns=PORTFOLIO_COLUMNS)
        
    port_df = _ensure_columns(port_df, PORTFOLIO_COLUMNS)
    
    # [에러 방어 핵심] 기존 데이터에 계좌 종류(Account_Type)가 없어서 나는 에러 원천 차단
    if not port_df.empty:
        port_df['Account_Type'] = port_df['Account_Type'].fillna('일반').astype(str).replace({'<NA>': '일반', 'nan': '일반', 'None': '일반', '': '일반'})
        
        # 다른 주요 문자열 컬럼들도 안전하게 문자열 치환
        for c in ["Owner", "Broker", "Ticker", "Stock_Name"]:
            port_df[c] = port_df[c].fillna('미입력').astype(str).replace({'<NA>': '미입력', 'nan': '미입력', 'None': '미입력'})

    def auto_fill_port_liquidity(row):
        if pd.isna(row.get('Liquidity')) or str(row.get('Liquidity')).strip() in ["", "nan", "<NA>", "미입력"]:
            if any(k in str(row.get('Account_Type', '')) for k in ['연금', 'IRP']):
                return "비유동"
            return "유동"
        return str(row.get('Liquidity', '유동')).strip()
        
    if not port_df.empty:
        port_df['Liquidity'] = port_df.apply(auto_fill_port_liquidity, axis=1)

    port_df = safe_to_numeric(port_df, "Shares")
    port_df = safe_to_numeric(port_df, "Avg_Price")
    if port_df.empty:
        return port_df
    
    def is_us_stock(ticker):
        t = str(ticker).strip().upper()
        if t in ["CASH", "미입력"] or not t or t == "NAN": return False
        return not (len(t) == 6 and t[0].isdigit())

    port_df['Is_US'] = port_df['Ticker'].apply(is_us_stock)
    port_df['Current_Price'] = port_df['Ticker'].apply(get_current_price)
    port_df['Avg_Price_KRW'] = port_df['Avg_Price'] 
    
    def calc_invested(row):
        if str(row['Ticker']).strip().upper() == 'CASH':
            return row['Avg_Price'] if row['Shares'] == 0 else row['Shares'] * row['Avg_Price']
        return row['Shares'] * row['Avg_Price_KRW']

    def calc_current_value(row):
        if str(row['Ticker']).strip().upper() == 'CASH':
            return row['Avg_Price'] if row['Shares'] == 0 else row['Shares'] * row['Avg_Price']
        return row['Shares'] * row['Current_Price']

    port_df['Total_Invested'] = port_df.apply(calc_invested, axis=1)
    port_df['Current_Value'] = port_df.apply(calc_current_value, axis=1)
    port_df['Profit_Amt'] = port_df['Current_Value'] - port_df['Total_Invested']
    
    return port_df

def color_profit(val):
    if pd.isna(val): return ''
    if isinstance(val, (int, float)):
        if val > 0:
            return 'color: #d32f2f; font-weight: bold;' 
        elif val < 0:
            return 'color: #1976d2; font-weight: bold;' 
    return ''

def highlight_total_row(row):
    if row['Broker'] == '🌟 합계':
        return ['background-color: #fffde7; font-weight: bold;'] * len(row) 
    return [''] * len(row)

# --- 앱 시작 ---
st.set_page_config(page_title="Family Asset Manager", layout="wide")

st.sidebar.header("🎯 금융자산 목표 설정")
target_fin_goal = st.sidebar.number_input("목표 금액 (억원)", value=10.0, step=1.0) * EOK
target_date = st.sidebar.date_input("목표 달성 기준일", value=date(2033, 12, 31))

days_left = (target_date - date.today()).days
st.sidebar.info(f"📍 목표일까지 **{days_left:,}일** 남았습니다.")

tabs = st.tabs(["📊 대시보드", "📝 일괄 관리(수기)", "📈 포트폴리오(자동)", "📈 시뮬레이션"])

# --- 1. 대시보드 ---
with tabs[0]:
    df_hist = load_history()
    live_port = get_live_portfolio()
    df_real = load_realized()
    
    if df_hist.empty and live_port.empty:
        st.warning("데이터가 없습니다. 자산을 등록해주세요.")
    else:
        if not df_hist.empty:
            latest_dt = df_hist['Record_DT'].max()
            df_latest_manual = df_hist[(df_hist['Record_DT'] == latest_dt) & (df_hist['Category'] != "금융자산(자동)")].copy()
        else:
            df_latest_manual = pd.DataFrame(columns=ASSET_COLUMNS)
        
        manual_assets = df_latest_manual[df_latest_manual["Category"] != "부채"]["Amount"].sum()
        manual_debts = df_latest_manual[df_latest_manual["Category"] == "부채"]["Amount"].sum()
        manual_fin = df_latest_manual[df_latest_manual["Category"] == "금융자산(수기)"]["Amount"].sum()
        
        live_fin_asset = live_port['Current_Value'].sum() if not live_port.empty else 0
        live_fin_profit = live_port['Profit_Amt'].sum() if not live_port.empty else 0
        
        total_assets = manual_assets + live_fin_asset
        total_debts = manual_debts
        net_worth = total_assets - total_debts
        
        financial_assets = manual_fin + live_fin_asset
        financial_profits = live_fin_profit + df_latest_manual[df_latest_manual["Category"] == "금융자산(수기)"]["Profit"].sum()

        def get_owner_stats(owner_name):
            port_inv = live_port[live_port['Owner'] == owner_name]['Total_Invested'].sum() if not live_port.empty else 0
            port_prof = live_port[live_port['Owner'] == owner_name]['Profit_Amt'].sum() if not live_port.empty else 0
            
            man_df = df_latest_manual[(df_latest_manual["Category"] == "금융자산(수기)") & (df_latest_manual["Owner"] == owner_name)]
            man_prof = man_df["Profit"].sum()
            man_inv = man_df["Amount"].sum() - man_prof
            
            tot_inv = port_inv + man_inv
            tot_prof = port_prof + man_prof
            
            rate = (tot_prof / tot_inv * 100) if tot_inv > 0 else 0
            color = "#d32f2f" if rate > 0 else "#1976d2" if rate < 0 else "#555"
            return rate, color
            
        wife_rate, wife_color = get_owner_stats("본인")
        husband_rate, husband_color = get_owner_stats("남편")

        # 상단 요약 카드 
        principal = financial_assets - financial_profits
        fin_return_rate = (financial_profits / principal * 100) if principal > 0 else 0
        profit_color = "#d32f2f" if financial_profits > 0 else "#1976d2" 
        profit_display = f"{financial_profits/EOK:,.2f}억" if abs(financial_profits) >= EOK else f"{financial_profits/10000:,.0f}만"

        st.markdown(f"""
        <div style="display: flex; flex-wrap: wrap; gap: 10px; text-align: center; margin-bottom: 20px;">
            <div style="flex: 1 1 30%; min-width: 140px; padding: 15px; border-radius: 8px; background-color: #ffffff; border: 1px solid #e0e0e0;">
                <p style="margin: 0; font-size: 13px; color: #757575;">현재 순자산 (부채 차감)</p>
                <p style="margin: 5px 0 0 0; font-size: 22px; font-weight: 800; color: #424242;">{net_worth/EOK:,.2f} 억</p>
            </div>
            <div style="flex: 1 1 30%; min-width: 140px; padding: 15px; border-radius: 8px; background-color: #ffffff; border: 1px solid #e0e0e0;">
                <p style="margin: 0; font-size: 13px; color: #757575;">금융자산 합계</p>
                <p style="margin: 5px 0 0 0; font-size: 22px; font-weight: 800; color: #1565c0;">{financial_assets/EOK:,.2f} 억</p>
            </div>
            <div style="flex: 1 1 30%; min-width: 200px; padding: 15px; border-radius: 8px; background-color: #ffffff; border: 1px solid #e0e0e0;">
                <p style="margin: 0; font-size: 13px; color: #757575;">총 수익률 (미실현+실현)</p>
                <p style="margin: 5px 0 8px 0; font-size: 22px; font-weight: 800; color: {profit_color};">
                    {fin_return_rate:.2f}% <span style="font-size: 16px; font-weight: bold;">({profit_display})</span>
                </p>
                <div style="display: flex; justify-content: space-around; border-top: 1px solid #eeeeee; padding-top: 8px; margin-top: 5px;">
                    <span style="font-size: 13px; color: #616161;">👩 본인: <strong style="color: {wife_color};">{wife_rate:.1f}%</strong></span>
                    <span style="font-size: 13px; color: #616161;">👨 남편: <strong style="color: {husband_color};">{husband_rate:.1f}%</strong></span>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # --- 🚨 절세 및 실현 손익 현황판 (Tax Radar) ---
        st.markdown("### 🚨 세금 알리미 & 절세 계좌 현황")
        
        curr_year = str(date.today().year)
        df_real['Year'] = pd.to_datetime(df_real['Date'], errors='coerce').dt.year.astype(str)
        df_this_year = df_real[df_real['Year'] == curr_year]
        
        os_profit = df_this_year[df_this_year['Category'] == '해외주식매도']['Amount'].sum()
        os_tax = max(0, (os_profit - 2500000) * 0.22)
        os_pct = min(100, (os_profit / 2500000) * 100) if os_profit > 0 else 0
        os_color = "#e53935" if os_profit > 2500000 else "#43a047"
        
        fin_income = df_this_year[df_this_year['Category'] == '배당/이자']['Amount'].sum()
        fin_pct = min(100, (fin_income / 20000000) * 100) if fin_income > 0 else 0
        fin_color = "#e53935" if fin_income > 20000000 else "#fdd835" if fin_income > 15000000 else "#1e88e5"

        isa_cont = df_this_year[df_this_year['Category'] == 'ISA납입']['Amount'].sum()
        isa_pct = min(100, (isa_cont / 20000000) * 100) if isa_cont > 0 else 0
        pen_cont = df_this_year[df_this_year['Category'] == '연금납입']['Amount'].sum()
        pen_pct = min(100, (pen_cont / 9000000) * 100) if pen_cont > 0 else 0

        st.markdown(f"""
        <div style="background-color: #fcfcfc; border: 1px solid #e0e0e0; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
            <div style="display: flex; flex-wrap: wrap; gap: 20px;">
                <div style="flex: 1; min-width: 250px;">
                    <p style="margin: 0 0 5px 0; font-size: 14px; font-weight: bold; color: #424242;">🌍 해외주식 양도소득세 (250만 원 공제)</p>
                    <div style="background-color: #eeeeee; border-radius: 5px; height: 10px; width: 100%; margin-bottom: 5px;">
                        <div style="background-color: {os_color}; width: {os_pct}%; height: 100%; border-radius: 5px;"></div>
                    </div>
                    <p style="margin: 0 0 15px 0; font-size: 12px; color: #757575;">
                        실현 수익: {os_profit/10000:,.0f}만 원 <strong style="color:{os_color};">(예상 세금: {os_tax/10000:,.0f}만 원)</strong>
                    </p>
                    
                    <p style="margin: 0 0 5px 0; font-size: 14px; font-weight: bold; color: #424242;">💰 금융소득종합과세 (2,000만 원 한도)</p>
                    <div style="background-color: #eeeeee; border-radius: 5px; height: 10px; width: 100%; margin-bottom: 5px;">
                        <div style="background-color: {fin_color}; width: {fin_pct}%; height: 100%; border-radius: 5px;"></div>
                    </div>
                    <p style="margin: 0; font-size: 12px; color: #757575;">올해 누적 배당/이자: {fin_income/10000:,.0f}만 원</p>
                </div>
                <div style="flex: 1; min-width: 250px; border-left: 1px solid #eeeeee; padding-left: 20px;">
                    <p style="margin: 0 0 5px 0; font-size: 14px; font-weight: bold; color: #424242;">🛡️ ISA 올해 납입 한도 (2,000만 원)</p>
                    <div style="background-color: #eeeeee; border-radius: 5px; height: 10px; width: 100%; margin-bottom: 5px;">
                        <div style="background-color: #8e24aa; width: {isa_pct}%; height: 100%; border-radius: 5px;"></div>
                    </div>
                    <p style="margin: 0 0 15px 0; font-size: 12px; color: #757575;">납입액: {isa_cont/10000:,.0f}만 원 (잔여 {max(0, 20000000-isa_cont)/10000:,.0f}만 원)</p>
                    
                    <p style="margin: 0 0 5px 0; font-size: 14px; font-weight: bold; color: #424242;">🏛️ 연금저축/IRP 세액공제 (900만 원)</p>
                    <div style="background-color: #eeeeee; border-radius: 5px; height: 10px; width: 100%; margin-bottom: 5px;">
                        <div style="background-color: #00897b; width: {pen_pct}%; height: 100%; border-radius: 5px;"></div>
                    </div>
                    <p style="margin: 0; font-size: 12px; color: #757575;">납입액: {pen_cont/10000:,.0f}만 원 (추가 필요 {max(0, 9000000-pen_cont)/10000:,.0f}만 원)</p>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        st.divider()

        trend_df = pd.DataFrame()
        if not df_hist.empty:
            trend_df = df_hist.groupby('Record_Date').apply(lambda x: pd.Series({
                '총자산': x[x['Category'] != '부채']['Amount'].sum(),
                '총부채': x[x['Category'] == '부채']['Amount'].sum()
            })).reset_index()
            trend_df['순자산'] = trend_df['총자산'] - trend_df['총부채']
            trend_df = trend_df.sort_values('Record_Date')

        tab_chart1, tab_chart2, tab_chart3 = st.tabs(["자산 추이", "포트폴리오 효율", "계좌별 비중"]) 

        with tab_chart1:
            if not trend_df.empty:
                fig_trend = go.Figure()
                fig_trend.add_trace(go.Bar(x=trend_df['Record_Date'], y=trend_df['총자산']/EOK, name='자산', marker_color='#81c784'))
                fig_trend.add_trace(go.Bar(x=trend_df['Record_Date'], y=-trend_df['총부채']/EOK, name='부채', marker_color='#cfd8dc'))
                fig_trend.add_trace(go.Scatter(
                    x=trend_df['Record_Date'], y=trend_df['순자산']/EOK, mode='lines+markers+text', 
                    text=(trend_df['순자산']/EOK).apply(lambda x: f"{x:,.1f}억"), textposition="top center", name='순자산',
                    textfont=dict(size=11, color='#2e7d32', weight='bold'),
                    line=dict(color='#2e7d32', width=2), marker=dict(size=6, color='white', line=dict(width=1.5, color='#2e7d32'))
                ))
                fig_trend.update_layout(height=300, barmode='relative', xaxis_type='category', hovermode="x unified", plot_bgcolor='white', paper_bgcolor='white', legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1), margin=dict(l=0, r=0, t=30, b=0))
                st.plotly_chart(fig_trend, use_container_width=True)

        with tab_chart2:
            profit_data = []
            if not live_port.empty:
                for _, r in live_port.iterrows():
                    profit_data.append({'Owner': r['Owner'], 'Broker': r['Broker'], 'Invested': r['Total_Invested'], 'Profit': r['Profit_Amt']})
            df_profit = pd.DataFrame(profit_data)
            if not df_profit.empty:
                df_profit_agg = df_profit.groupby(['Owner', 'Broker'])[['Invested', 'Profit']].sum().reset_index()
                df_profit_agg['Return(%)'] = (df_profit_agg['Profit'] / df_profit_agg['Invested'] * 100).fillna(0)
                df_profit_agg['Invested_disp'] = df_profit_agg['Invested'].apply(lambda x: max(abs(x), 100_000)) 
                
                fig_bubble = px.scatter(df_profit_agg, x='Return(%)', y='Profit', size='Invested_disp', color='Owner', text='Broker', hover_name='Broker', hover_data={'Owner': False, 'Broker': False, 'Invested_disp': False, 'Invested': ':,.0f', 'Profit': ':,.0f', 'Return(%)': ':.1f'}, color_discrete_sequence=px.colors.qualitative.Pastel)
                fig_bubble.update_traces(textposition='top center', textfont=dict(size=11, color='#424242', weight='bold'), marker=dict(line=dict(width=1, color='DarkSlateGrey'), opacity=0.8))
                fig_bubble.add_hline(y=0, line_dash="solid", line_color="#e0e0e0", line_width=1)
                fig_bubble.add_vline(x=0, line_dash="solid", line_color="#e0e0e0", line_width=1)
                fig_bubble.update_layout(height=300, xaxis_title="수익률 (%)", yaxis_title="수익금", plot_bgcolor='white', paper_bgcolor='white', legend_title="소유자", margin=dict(l=0, r=0, t=30, b=0))
                st.plotly_chart(fig_bubble, use_container_width=True)

        with tab_chart3:
            if not live_port.empty:
                # [에러 방어 핵심 2] 빈 값이 하나라도 있으면 Plotly Sunburst가 충돌하므로 완전한 데이터만 필터링해서 그림
                plot_df = live_port.dropna(subset=['Owner', 'Account_Type', 'Broker'])
                plot_df = plot_df[(plot_df['Owner'] != '') & (plot_df['Account_Type'] != '') & (plot_df['Broker'] != '')]
                
                if not plot_df.empty:
                    fig_acc = px.sunburst(plot_df, path=['Owner', 'Account_Type', 'Broker'], values='Current_Value', color='Account_Type', color_discrete_sequence=px.colors.qualitative.Set3)
                    fig_acc.update_traces(textinfo="label+percent root", insidetextorientation='radial')
                    fig_acc.update_layout(height=280, margin=dict(l=0, r=0, t=0, b=0))
                    st.plotly_chart(fig_acc, use_container_width=True)

# --- 2. 자산 일괄 관리 ---
with tabs[1]:
    st.markdown("#### 📝 수기 자산 관리")
    st.info("부동산, 예적금, 부채 등을 관리합니다. 주식/현금은 [포트폴리오] 탭을 이용하세요.")
    
    df_hist = load_history()
    editor_df = df_hist[df_hist['Category'] != '금융자산(자동)'].copy()
    editor_df = editor_df.drop(columns=["Record_DT", "Record_Month"], errors="ignore")
    
    with st.form("manual_asset_form"):
        edited_df = st.data_editor(editor_df, num_rows="dynamic", use_container_width=True, height=300, column_config={
            "Record_Date": st.column_config.TextColumn("날짜", required=True),
            "Owner": st.column_config.SelectboxColumn("소유자", options=["본인", "남편", "공동"]),
            "Category": st.column_config.SelectboxColumn("분류", options=["부동산", "금융자산(수기)", "부채", "기타"]),
            "Sub_Category": st.column_config.TextColumn("상세 항목"),
            "Liquidity": st.column_config.SelectboxColumn("유동성", options=["유동", "비유동"]),
            "Amount": st.column_config.NumberColumn("금액(원)", format="%,d"),
            "Profit": st.column_config.NumberColumn("수익(원)", format="%,d")
        })
        if st.form_submit_button("💾 수기 데이터 최종 저장"):
            auto_df = df_hist[df_hist['Category'] == '금융자산(자동)'].drop(columns=["Record_DT", "Record_Month"], errors="ignore")
            final_save_df = pd.concat([edited_df, auto_df], ignore_index=True)
            save_history(final_save_df)
            st.success("저장되었습니다.")
            st.rerun()

# --- 3. 주식/포트폴리오 관리 (세금 관리 포함) ---
with tabs[2]:
    st.markdown("#### 📈 주식 및 계좌 포트폴리오")
    st.info("💡 **계좌 종류**를 정확히 선택하세요. 현금 예수금은 수량을 0으로 두고 평균매수가에 총액을 적습니다.")
    
    port_df = get_live_portfolio().drop(columns=['Is_US', 'Avg_Price_KRW', 'Current_Price', 'Total_Invested', 'Current_Value', 'Profit_Amt'], errors='ignore')
    if "LastUpdated" not in port_df.columns: port_df["LastUpdated"] = ""
    
    with st.form("portfolio_form"):
        edited_port = st.data_editor(port_df, num_rows="dynamic", use_container_width=True, height=250, column_config={
            "Owner": st.column_config.SelectboxColumn("소유자", options=["본인", "남편", "공동"]),
            "Broker": st.column_config.TextColumn("증권사", required=True),
            "Account_Type": st.column_config.SelectboxColumn("계좌 종류", options=["일반", "ISA", "연금저축", "IRP", "비과세", "기타"], required=True),
            "Ticker": st.column_config.TextColumn("종목코드", required=True),
            "Stock_Name": st.column_config.TextColumn("종목명", required=True),
            "Liquidity": st.column_config.SelectboxColumn("유동성", options=["유동", "비유동"]),
            "Shares": st.column_config.NumberColumn("수량", format="%,d", min_value=0),
            "Avg_Price": st.column_config.NumberColumn("평단가(원)", min_value=0.0),
            "LastUpdated": st.column_config.TextColumn("최근수정일시", disabled=True)
        })
        
        col1, col2 = st.columns([1, 1])
        with col1:
            if st.form_submit_button("💾 포트폴리오 저장", use_container_width=True):
                current_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for idx, row in edited_port.iterrows():
                    if idx not in port_df.index:
                        edited_port.at[idx, 'LastUpdated'] = current_ts
                    else:
                        old_row = port_df.loc[idx]
                        if any(str(row[c]) != str(old_row[c]) for c in ["Owner", "Broker", "Account_Type", "Ticker", "Shares", "Avg_Price"]):
                            edited_port.at[idx, 'LastUpdated'] = current_ts
                        else:
                            edited_port.at[idx, 'LastUpdated'] = old_row.get('LastUpdated', current_ts)
                save_portfolio(edited_port)
                st.success("저장되었습니다.")
                st.rerun()
        with col2:
            if st.form_submit_button("📸 오늘 스냅샷 찍기", type="primary", use_container_width=True):
                df_h = load_history()
                live_p = get_live_portfolio()
                tdy = date.today().strftime("%Y-%m-%d")
                df_h = df_h[df_h['Record_Date'] != tdy]
                recent_manual = df_h[(df_h['Record_DT'] == df_h['Record_DT'].max()) & (df_h['Category'] != "금융자산(자동)")].copy() if not df_h.empty else pd.DataFrame()
                if not recent_manual.empty: recent_manual['Record_Date'] = tdy
                
                auto_s = pd.DataFrame({"Record_Date": tdy, "Owner": live_p['Owner'], "Category": "금융자산(자동)", "Sub_Category": live_p['Broker'] + " (" + live_p['Stock_Name'] + ")", "Liquidity": live_p['Liquidity'], "Amount": live_p['Current_Value'], "Profit": live_p['Profit_Amt'], "Note": "자동연동"}) if not live_p.empty else pd.DataFrame()
                save_history(pd.concat([df_h.drop(columns=["Record_DT", "Record_Month"], errors='ignore'), recent_manual.drop(columns=["Record_DT", "Record_Month"], errors='ignore'), auto_s], ignore_index=True))
                st.balloons()

    st.divider()
    
    st.markdown("##### 📥 실현 손익 및 절세/배당 기록부")
    st.info("주식을 매도하여 이익을 확정했거나, 배당금 수령, 연금/ISA에 현금을 납입했을 때 가끔씩 뭉텅이로 입력해두면 대시보드에서 세금을 계산해 줍니다.")
    
    real_df = load_realized()
    with st.form("realized_form"):
        edited_real = st.data_editor(real_df, num_rows="dynamic", use_container_width=True, height=200, column_config={
            "Date": st.column_config.TextColumn("입력월/날짜 (예: 2026-04)", required=True),
            "Owner": st.column_config.SelectboxColumn("소유자", options=["본인", "남편", "공동"]),
            "Category": st.column_config.SelectboxColumn("구분", options=["해외주식매도", "국내주식매도", "배당/이자", "ISA납입", "연금납입"]),
            "Item": st.column_config.TextColumn("항목(종목명 또는 증권사)"),
            "Amount": st.column_config.NumberColumn("금액(원)", format="%,d", required=True),
            "Note": st.column_config.TextColumn("비고")
        })
        if st.form_submit_button("📝 실현 손익/납입액 저장"):
            save_realized(edited_real)
            st.success("세금 및 실현 손익 데이터가 반영되었습니다.")
            st.rerun()

# --- 4. 은퇴 시뮬레이션 ---
with tabs[3]:
    st.markdown("#### 은퇴 로드맵 시뮬레이터")
    c_age = 43 
    t_age = st.sidebar.number_input("은퇴 목표 나이", value=50, min_value=c_age + 1)
    m_inv = st.sidebar.number_input("월 추가 투자금 (만원)", value=250)

    if st.button("장기 시뮬레이션 실행", use_container_width=True):
        months = (t_age - c_age) * 12
        curr_fin = financial_assets if 'financial_assets' in locals() else 0
        m_rate = (1 + 0.06) ** (1/12) 
        results = []
        temp_f = curr_fin
        for _ in range(months):
            temp_f = (temp_f * m_rate) + (m_inv * 10_000)
            results.append(temp_f)
        fig_sim = px.area(y=results, title=f"{t_age}세 은퇴 시 예측 (최종: {results[-1]/EOK:,.1f}억)")
        fig_sim.update_layout(height=300, margin=dict(l=0, r=0, t=30, b=0))
        st.plotly_chart(fig_sim, use_container_width=True)
