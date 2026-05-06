import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, date
import os
import FinanceDataReader as fdr
from streamlit_gsheets import GSheetsConnection 

ASSET_COLUMNS = ["Record_Date", "Owner", "Category", "Sub_Category", "Liquidity", "Amount", "Profit", "Note"]
SIM_COLUMNS = ["Sim_Date", "Target_Age", "Monthly_Investment", "Result_Final_Asset"]
PORTFOLIO_COLUMNS = ["Owner", "Broker", "Ticker", "Stock_Name", "Liquidity", "Shares", "Avg_Price"]

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
        for c in ["Owner", "Category", "Sub_Category", "Liquidity", "Note"]:
            df[c] = df[c].astype(str).replace('<NA>', '').replace('nan', '')
        
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
    
    def auto_fill_port_liquidity(row):
        if pd.isna(row.get('Liquidity')) or str(row.get('Liquidity')).strip() in ["", "nan", "<NA>"]:
            if any(k in str(row.get('Broker', '')) for k in ['연금', 'IRP', '퇴직']):
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
        if t == "CASH" or not t or t == "NAN": return False
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

# --- 데이터프레임 수익률 컬러링 공통 함수 ---
def color_profit(val):
    if pd.isna(val): return ''
    if isinstance(val, (int, float)):
        if val > 0:
            return 'color: #d32f2f; font-weight: bold;' 
        elif val < 0:
            return 'color: #1976d2; font-weight: bold;' 
    return ''

def highlight_total_row(row):
    if row['Broker'] == '🌟 전체 합계':
        return ['background-color: #fffde7; font-weight: bold;'] * len(row) 
    return [''] * len(row)

# --- 앱 시작 ---
st.set_page_config(page_title="Family Asset Manager", layout="wide")

st.sidebar.header("🎯 금융자산 목표 설정")
target_fin_goal = st.sidebar.number_input("목표 금액 (억원)", value=10.0, step=1.0) * EOK
target_date = st.sidebar.date_input("목표 달성 기준일", value=date(2033, 12, 31))

days_left = (target_date - date.today()).days
st.sidebar.info(f"📍 목표일까지 **{days_left:,}일** 남았습니다.")

tabs = st.tabs(["📊 대시보드", "📝 자산 일괄 관리(수기)", "📈 주식/포트폴리오 관리", "📈 은퇴 시뮬레이션"])

# --- 1. 대시보드 ---
with tabs[0]:
    df_hist = load_history()
    live_port = get_live_portfolio()
    
    if df_hist.empty and live_port.empty:
        st.warning("데이터가 없습니다. 자산을 등록해주세요. (초기 설정 중이시라면 정상입니다!)")
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

        summary_message = "첫 자산 스냅샷을 기록하시면 다음부터 이전 기록과 비교해 드릴게요!"
        trend_df = pd.DataFrame()
        if not df_hist.empty:
            trend_df = df_hist.groupby('Record_Date').apply(lambda x: pd.Series({
                '총자산': x[x['Category'] != '부채']['Amount'].sum(),
                '총부채': x[x['Category'] == '부채']['Amount'].sum()
            })).reset_index()
            trend_df['순자산'] = trend_df['총자산'] - trend_df['총부채']
            trend_df = trend_df.sort_values('Record_Date')

            if len(trend_df) >= 1:
                today_str = date.today().strftime("%Y-%m-%d")
                if len(trend_df) >= 2 and trend_df.iloc[-1]['Record_Date'] == today_str:
                    past_record = trend_df.iloc[-2]
                else:
                    past_record = trend_df.iloc[-1]
                
                past_date = past_record['Record_Date']
                past_nw = past_record['순자산']
                diff = net_worth - past_nw
                
                if diff > 0:
                    summary_message = f"🎉 지난번 기록({past_date}) 대비 순자산이 <strong style='color:#d32f2f;'>{diff/EOK:,.2f}억원 증가</strong>하며 우상향 순항 중입니다!"
                elif diff < 0:
                    summary_message = f"💡 지난번 기록({past_date}) 대비 순자산이 <strong style='color:#1976d2;'>{abs(diff)/EOK:,.2f}억원 감소</strong>했습니다. 잠시 쉬어가는 구간이네요!"
                else:
                    summary_message = f"⚖️ 지난번 기록({past_date})과 순자산 규모가 동일하게 유지되고 있습니다."

        st.markdown("### 💎 현재 자산 요약")
        st.markdown(f"""
        <div style="background-color: #f8f9fa; border-left: 5px solid #2e7d32; padding: 15px; margin-bottom: 20px; border-radius: 0 5px 5px 0;">
            <span style="font-size: 16px; color: #333;">{summary_message}</span>
        </div>
        """, unsafe_allow_html=True)
        
        principal = financial_assets - financial_profits
        fin_return_rate = (financial_profits / principal * 100) if principal > 0 else 0
        profit_color = "#d32f2f" if financial_profits > 0 else "#1976d2" 

        if abs(financial_profits) >= EOK:
            profit_display = f"{financial_profits/EOK:,.2f}억원"
        else:
            profit_display = f"{financial_profits/10000:,.0f}만원"

        st.markdown(f"""
        <div style="display: flex; gap: 20px; text-align: center; margin-bottom: 30px;">
            <div style="flex: 1; padding: 20px; border-radius: 10px; background-color: #ffffff; border: 1px solid #e0e0e0; box-shadow: 0 2px 4px rgba(0,0,0,0.05);">
                <p style="margin: 0; font-size: 16px; color: #757575;">현재 순자산 (부채 차감)</p>
                <p style="margin: 5px 0 0 0; font-size: 32px; font-weight: 800; color: #424242;">{net_worth/EOK:,.2f} 억</p>
            </div>
            <div style="flex: 1; padding: 20px; border-radius: 10px; background-color: #ffffff; border: 1px solid #e0e0e0; box-shadow: 0 2px 4px rgba(0,0,0,0.05);">
                <p style="margin: 0; font-size: 16px; color: #757575;">금융자산 합계</p>
                <p style="margin: 5px 0 0 0; font-size: 32px; font-weight: 800; color: #1565c0;">{financial_assets/EOK:,.2f} 억</p>
            </div>
            <div style="flex: 1; padding: 20px; border-radius: 10px; background-color: #ffffff; border: 1px solid #e0e0e0; box-shadow: 0 2px 4px rgba(0,0,0,0.05);">
                <p style="margin: 0; font-size: 16px; color: #757575;">금융자산 총 수익률</p>
                <p style="margin: 5px 0 10px 0; font-size: 32px; font-weight: 800; color: {profit_color};">
                    {fin_return_rate:.2f}% <span style="font-size: 20px; font-weight: bold;">({profit_display})</span>
                </p>
                <div style="display: flex; justify-content: space-around; border-top: 1px solid #eeeeee; padding-top: 12px; margin-top: 10px;">
                    <span style="font-size: 15px; color: #616161;">👩 본인: <strong style="color: {wife_color};">{wife_rate:.2f}%</strong></span>
                    <span style="font-size: 15px; color: #616161;">👨 남편: <strong style="color: {husband_color};">{husband_rate:.2f}%</strong></span>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # --- 개편된 목표 달성 현황 섹션 ---
        st.markdown("### 🚩 목표 달성 현황 및 향후 시뮬레이션")
        
        today_date = date.today()
        months_left = (target_date.year - today_date.year) * 12 + target_date.month - today_date.month
        if target_date.day < today_date.day:
            months_left -= 1
        months_left = max(1, months_left) # 0으로 나누기 방지
        
        remaining_asset = max(0, target_fin_goal - financial_assets)
        required_monthly_savings = remaining_asset / months_left
        progress_pct = min(1.0, financial_assets / target_fin_goal) * 100

        st.markdown(f"""
        <div style="background-color: #ffffff; border: 1px solid #eeeeee; border-radius: 8px; padding: 25px; text-align: center; margin-bottom: 30px; box-shadow: 0 2px 4px rgba(0,0,0,0.05);">
            <p style="font-size: 18px; color: #424242; margin-bottom: 10px;">목표 달성일인 <strong>{target_date.strftime('%Y년 %m월')}</strong>까지 <strong>{months_left}개월</strong> 남았습니다.</p>
            <p style="font-size: 16px; color: #757575; margin-bottom: 20px;">
                목표 금액 <strong>{target_fin_goal/EOK:.1f}억</strong> 중 현재 <strong>{financial_assets/EOK:.2f}억</strong> (달성률 {progress_pct:.1f}%)
            </p>
            <hr style="border-top: 1px dashed #e0e0e0; margin: 20px 0;"/>
            <h4 style="color: #1976d2; margin: 0; font-weight: 600;">
                💡 목표 달성을 위해 매월 <span style="font-size: 26px; color: #d32f2f;">{required_monthly_savings/10000:,.0f}만 원</span>을 추가로 투자해야 합니다.
            </h4>
            <p style="font-size: 13px; color: #9e9e9e; margin-top: 10px;">* 단순 원금 누적 기준 계산 (수익률 변동 미반영)</p>
        </div>
        """, unsafe_allow_html=True)

        st.divider()

        # --- 차트 섹션 ---
        st.markdown("### 📈 상세 대시보드")
        tab_chart1, tab_chart2, tab_chart3 = st.tabs(["자산 추이", "소유/증권사별 수익", "자산 구성 비중"]) 

        with tab_chart1:
            if not trend_df.empty:
                fig_trend = go.Figure()
                fig_trend.add_trace(go.Bar(x=trend_df['Record_Date'], y=trend_df['총자산']/EOK, name='자산', marker_color='#81c784'))
                fig_trend.add_trace(go.Bar(x=trend_df['Record_Date'], y=-trend_df['총부채']/EOK, name='부채', marker_color='#cfd8dc'))
                fig_trend.add_trace(go.Scatter(
                    x=trend_df['Record_Date'], y=trend_df['순자산']/EOK, mode='lines+markers+text', 
                    text=(trend_df['순자산']/EOK).apply(lambda x: f"{x:,.1f}억"),
                    textposition="top center", name='순자산',
                    textfont=dict(size=13, color='#2e7d32', weight='bold'),
                    line=dict(color='#2e7d32', width=3), marker=dict(size=8, color='white', line=dict(width=2, color='#2e7d32'))
                ))
                fig_trend.update_layout(
                    height=450, 
                    barmode='relative', 
                    xaxis_type='category', # [수정] 스냅샷이 있는 날짜만 표시되도록 강제 지정
                    hovermode="x unified", 
                    plot_bgcolor='white', 
                    paper_bgcolor='white',
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1), 
                    margin=dict(l=0, r=0, t=30, b=0)
                )
                fig_trend.update_xaxes(showgrid=False)
                fig_trend.update_yaxes(showgrid=True, gridcolor='#f5f5f5')
                st.plotly_chart(fig_trend, use_container_width=True)
            else:
                st.info("자산 스냅샷 데이터가 충분하지 않습니다.")

        with tab_chart2:
            st.markdown("<p style='font-size: 15px; color: #616161; margin-bottom: 5px;'>💡 <b>수익금(Y축)</b>과 <b>수익률(X축)</b>을 동시에 보여주는 <b>포트폴리오 효율성 차트</b>입니다.</p>", unsafe_allow_html=True)
            st.markdown("<p style='font-size: 13px; color: #9e9e9e; margin-top: 0;'>* 원의 크기는 투자 원금 비중이며, 1사분면(우상단)에 위치할수록 고수익금/고수익률 자산입니다.</p>", unsafe_allow_html=True)
            
            profit_data = []
            if not live_port.empty:
                for _, r in live_port.iterrows():
                    profit_data.append({'Owner': r['Owner'], 'Broker': r['Broker'], 'Invested': r['Total_Invested'], 'Profit': r['Profit_Amt']})
            if not df_latest_manual.empty:
                man_fin = df_latest_manual[df_latest_manual['Category'] == '금융자산(수기)']
                for _, r in man_fin.iterrows():
                    inv = r['Amount'] - r['Profit']
                    profit_data.append({'Owner': r['Owner'], 'Broker': r['Sub_Category'], 'Invested': inv, 'Profit': r['Profit']})
            
            df_profit = pd.DataFrame(profit_data)
            if not df_profit.empty:
                df_profit_agg = df_profit.groupby(['Owner', 'Broker'])[['Invested', 'Profit']].sum().reset_index()
                df_profit_agg['Return(%)'] = (df_profit_agg['Profit'] / df_profit_agg['Invested'] * 100).fillna(0)
                
                # 버블 차트 렌더링을 위해 최소 크기 보정 (시각적 오류 방지용)
                df_profit_agg['Invested_disp'] = df_profit_agg['Invested'].apply(lambda x: max(abs(x), 100_000)) 
                
                fig_bubble = px.scatter(
                    df_profit_agg, 
                    x='Return(%)', 
                    y='Profit', 
                    size='Invested_disp', 
                    color='Owner', 
                    text='Broker',
                    hover_name='Broker',
                    hover_data={'Owner': False, 'Broker': False, 'Invested_disp': False, 'Invested': ':,.0f', 'Profit': ':,.0f', 'Return(%)': ':.2f'},
                    color_discrete_sequence=px.colors.qualitative.Pastel
                )
                
                fig_bubble.update_traces(
                    textposition='top center', 
                    textfont=dict(size=12, color='#424242', weight='bold'),
                    marker=dict(line=dict(width=1, color='DarkSlateGrey'), opacity=0.8)
                )
                
                # 0을 기준으로 십자선 추가
                fig_bubble.add_hline(y=0, line_dash="solid", line_color="#e0e0e0", line_width=1.5)
                fig_bubble.add_vline(x=0, line_dash="solid", line_color="#e0e0e0", line_width=1.5)
                
                fig_bubble.update_layout(
                    height=450,
                    xaxis_title="수익률 (%)",
                    yaxis_title="수익금 (원)",
                    plot_bgcolor='white', 
                    paper_bgcolor='white',
                    legend_title="소유자",
                    margin=dict(l=0, r=0, t=30, b=0)
                )
                fig_bubble.update_xaxes(showgrid=True, gridcolor='#f5f5f5', zeroline=False)
                fig_bubble.update_yaxes(showgrid=True, gridcolor='#f5f5f5', zeroline=False)
                st.plotly_chart(fig_bubble, use_container_width=True)
                
                # 하단에 정확한 수치 확인을 위한 상세 표 추가
                st.markdown("##### 📋 증권사별 상세 수익 내역")
                disp_df = df_profit_agg[['Owner', 'Broker', 'Invested', 'Profit', 'Return(%)']].sort_values(by='Profit', ascending=False)
                disp_df.columns = ['소유자', '증권사(항목)', '투자원금', '수익금', '수익률(%)']
                
                styled_disp = disp_df.style.map(color_profit, subset=['수익금', '수익률(%)']).format({
                    '투자원금': '{:,.0f}', '수익금': '{:,.0f}', '수익률(%)': '{:.2f}%'
                })
                st.dataframe(styled_disp, use_container_width=True, hide_index=True)
            else:
                st.info("표시할 수익금 데이터가 없습니다.")
                
        with tab_chart3:
            if not df_latest_manual.empty or not live_port.empty:
                manual_fin_df = df_latest_manual[df_latest_manual["Category"] == "금융자산(수기)"].copy()
                auto_fin_df = pd.DataFrame()
                if not live_port.empty:
                    auto_fin_df = live_port[['Owner', 'Broker', 'Ticker', 'Current_Value', 'Liquidity']].copy()
                    auto_fin_df.rename(columns={'Broker': 'Sub_Category', 'Current_Value': 'Amount'}, inplace=True)
                    auto_fin_df['Category'] = "금융자산(자동)"

                combined_fin = pd.concat([manual_fin_df, auto_fin_df], ignore_index=True)
                combined_fin['Root'] = '전체 금융자산'

                col_sun1, col_sun2 = st.columns(2)
                with col_sun1:
                    st.markdown("<p style='text-align:center; font-weight:bold;'>💧 유동성 기준</p>", unsafe_allow_html=True)
                    fig_liq = px.sunburst(combined_fin, path=['Root', 'Owner', 'Liquidity'], values='Amount', color='Owner', color_discrete_sequence=px.colors.qualitative.Pastel)
                    fig_liq.update_traces(textinfo="label+percent root", insidetextorientation='radial')
                    fig_liq.update_layout(height=400, margin=dict(l=0, r=0, t=0, b=0))
                    st.plotly_chart(fig_liq, use_container_width=True)
                    
                with col_sun2:
                    st.markdown("<p style='text-align:center; font-weight:bold;'>🏢 증권사 기준</p>", unsafe_allow_html=True)
                    fig_broker = px.sunburst(combined_fin, path=['Root', 'Owner', 'Sub_Category'], values='Amount', color='Owner', color_discrete_sequence=px.colors.qualitative.Set3)
                    fig_broker.update_traces(textinfo="label+percent root", insidetextorientation='radial')
                    fig_broker.update_layout(height=400, margin=dict(l=0, r=0, t=0, b=0))
                    st.plotly_chart(fig_broker, use_container_width=True)

# --- 2. 자산 일괄 관리 ---
with tabs[1]:
    st.title("📝 수기 자산 관리 (부동산, 부채, 예적금 등)")
    st.info("⚠️ **주의:** 주식 및 현금 예수금은 이 탭이 아닌 **[주식/포트폴리오 관리]** 탭에서 관리하세요.")
    
    df_hist = load_history()
    editor_df = df_hist[df_hist['Category'] != '금융자산(자동)'].copy()
    editor_df = editor_df.drop(columns=["Record_DT", "Record_Month"], errors="ignore")
    
    with st.form("manual_asset_form"):
        edited_df = st.data_editor(
            editor_df, num_rows="dynamic", use_container_width=True, height=400,
            column_config={
                "Record_Date": st.column_config.TextColumn("날짜", required=True),
                "Owner": st.column_config.SelectboxColumn("소유자", options=["본인", "남편", "공동"]),
                "Category": st.column_config.SelectboxColumn("분류", options=["부동산", "금융자산(수기)", "부채", "기타"]),
                "Sub_Category": st.column_config.TextColumn("상세 항목"),
                "Liquidity": st.column_config.SelectboxColumn("유동성", options=["유동", "비유동"]),
                "Amount": st.column_config.NumberColumn("금액(원)", format="%,d"),
                "Profit": st.column_config.NumberColumn("수익(원)", format="%,d"),
            }
        )
        
        submitted_hist = st.form_submit_button("💾 수기 데이터 최종 저장")
        if submitted_hist:
            auto_df = df_hist[df_hist['Category'] == '금융자산(자동)'].drop(columns=["Record_DT", "Record_Month"], errors="ignore")
            final_save_df = pd.concat([edited_df, auto_df], ignore_index=True)
            save_history(final_save_df)
            st.success("수기 자산이 구글 시트에 저장되었습니다.")
            st.rerun()

# --- 3. 주식/포트폴리오 관리 ---
with tabs[2]:
    st.title("📈 주식 및 현금 포트폴리오 관리")
    
    col_port1, col_port2 = st.columns([2, 1])
    with col_port1:
        st.info("💡 **해외주식도 원화(KRW) 기준 평균매수가를 그대로 입력하세요!**\n**예수금(CASH)**은 수량을 0으로 두고, **'평균매수가'에 현금 총액**을 입력해 주세요.")
    with col_port2:
        if st.button("📸 오늘 날짜로 전체 자산 스냅샷 찍기", type="primary", use_container_width=True):
            df_hist_full = load_history()
            live_port_df = get_live_portfolio()
            today_str = date.today().strftime("%Y-%m-%d")
            
            df_hist_full = df_hist_full[df_hist_full['Record_Date'] != today_str]
            latest_dt = df_hist_full['Record_DT'].max() if not df_hist_full.empty else None
            
            new_snapshot = pd.DataFrame()
            if latest_dt:
                recent_manual = df_hist_full[(df_hist_full['Record_DT'] == latest_dt) & (df_hist_full['Category'] != "금융자산(자동)")].copy()
                recent_manual['Record_Date'] = today_str
                new_snapshot = pd.concat([new_snapshot, recent_manual])
            
            if not live_port_df.empty:
                auto_snap = pd.DataFrame({
                    "Record_Date": today_str,
                    "Owner": live_port_df['Owner'],
                    "Category": "금융자산(자동)",
                    "Sub_Category": live_port_df['Broker'] + " (" + live_port_df['Stock_Name'] + ")",
                    "Liquidity": live_port_df['Liquidity'],
                    "Amount": live_port_df['Current_Value'],
                    "Profit": live_port_df['Profit_Amt'],
                    "Note": "실시간 시세 연동"
                })
                new_snapshot = pd.concat([new_snapshot, auto_snap])
            
            if not new_snapshot.empty:
                new_snapshot = new_snapshot.drop(columns=["Record_DT", "Record_Month"], errors='ignore')
                df_hist_full = df_hist_full.drop(columns=["Record_DT", "Record_Month"], errors='ignore')
                save_history(pd.concat([df_hist_full, new_snapshot], ignore_index=True))
                st.balloons()
                st.success(f"{today_str} 기준 스냅샷이 성공적으로 기록되었습니다!")

    port_df = get_live_portfolio().drop(columns=['Is_US', 'Avg_Price_KRW', 'Current_Price', 'Total_Invested', 'Current_Value', 'Profit_Amt'], errors='ignore')
    
    with st.form("portfolio_form"):
        edited_port = st.data_editor(
            port_df, num_rows="dynamic", use_container_width=True, height=250,
            column_config={
                "Owner": st.column_config.SelectboxColumn("소유자", options=["본인", "남편", "공동"]),
                "Broker": st.column_config.TextColumn("증권사(예: KB증권)", required=True),
                "Ticker": st.column_config.TextColumn("종목코드(미국주식 예: AAPL)", required=True),
                "Stock_Name": st.column_config.TextColumn("종목명", required=True),
                "Liquidity": st.column_config.SelectboxColumn("유동성", options=["유동", "비유동"], required=True),
                "Shares": st.column_config.NumberColumn("보유수량", format="%,d", min_value=0),
                "Avg_Price": st.column_config.NumberColumn("평균매수가(원화 기준)", min_value=0.0),
            }
        )
        
        submitted_port = st.form_submit_button("💾 포트폴리오 저장")
        if submitted_port:
            save_portfolio(edited_port)
            st.success("포트폴리오가 구글 시트에 저장되었습니다.")
            st.rerun()
        
    st.divider()
    
    calc_df = get_live_portfolio()
    if not calc_df.empty:
        st.subheader("📊 실시간 포트폴리오 평가")
        calc_df['Return(%)'] = calc_df.apply(lambda x: (x['Profit_Amt'] / x['Total_Invested'] * 100) if x['Total_Invested'] > 0 else 0, axis=1)
        
        disp_df = calc_df.rename(columns={'Avg_Price_KRW': 'Avg_Price(KRW)'})
        disp_cols = ['Owner', 'Broker', 'Ticker', 'Stock_Name', 'Liquidity', 'Shares', 'Avg_Price(KRW)', 'Current_Price', 'Total_Invested', 'Current_Value', 'Profit_Amt', 'Return(%)']
        
        styled_disp = disp_df[disp_cols].style.map(color_profit, subset=['Profit_Amt', 'Return(%)']).format({
            'Shares': '{:,.0f}', 'Avg_Price(KRW)': '{:,.0f}', 'Current_Price': '{:,.0f}',
            'Total_Invested': '{:,.0f}', 'Current_Value': '{:,.0f}', 'Profit_Amt': '{:,.0f}', 'Return(%)': '{:.2f}%'
        })
        st.dataframe(styled_disp, use_container_width=True)
        
        st.markdown("#### 🏢 증권사별 합계 (예수금 포함)")
        summary = calc_df.groupby('Broker')[['Total_Invested', 'Current_Value']].sum().reset_index()
        summary['Total_Profit'] = summary['Current_Value'] - summary['Total_Invested']
        summary['Total_Return(%)'] = (summary['Total_Profit'] / summary['Total_Invested'] * 100).fillna(0)
        
        tot_inv = summary['Total_Invested'].sum()
        tot_val = summary['Current_Value'].sum()
        tot_prof = tot_val - tot_inv
        tot_ret = (tot_prof / tot_inv * 100) if tot_inv > 0 else 0
        
        total_row = pd.DataFrame([{
            'Broker': '🌟 전체 합계', 'Total_Invested': tot_inv, 'Current_Value': tot_val,
            'Total_Profit': tot_prof, 'Total_Return(%)': tot_ret
        }])
        summary = pd.concat([summary, total_row], ignore_index=True)
        
        styled_summary = summary.style.apply(highlight_total_row, axis=1) \
                                      .map(color_profit, subset=['Total_Profit', 'Total_Return(%)']) \
                                      .format({
                                          'Total_Invested': '{:,.0f}', 'Current_Value': '{:,.0f}',
                                          'Total_Profit': '{:,.0f}', 'Total_Return(%)': '{:.2f}%'
                                      })
        st.dataframe(styled_summary, use_container_width=True)

# --- 4. 은퇴 시뮬레이션 ---
with tabs[3]:
    st.title("은퇴 로드맵 시뮬레이터")
    c_age = 43 
    t_age = st.sidebar.number_input("은퇴 목표 나이", value=50, min_value=c_age + 1)
    m_inv = st.sidebar.number_input("월 추가 투자금 (만원)", value=250)

    if st.button("장기 시뮬레이션 실행"):
        months = (t_age - c_age) * 12
        curr_fin = financial_assets if 'financial_assets' in locals() else 0
        m_rate = (1 + 0.06) ** (1/12)
        results = []
        temp_f = curr_fin
        for _ in range(months):
            temp_f = (temp_f * m_rate) + (m_inv * 10_000)
            results.append(temp_f)
        fig_sim = px.area(y=results, title=f"{t_age}세 은퇴 시 예측 (최종: {results[-1]/EOK:,.2f}억)")
        st.plotly_chart(fig_sim, use_container_width=True)
