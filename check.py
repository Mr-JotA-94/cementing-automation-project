import pandas as pd
df = pd.read_csv('02_staging/sensor_data_clean.csv')
print(df[df['is_anomaly']==True]['fault_type'].value_counts())
print('Stuck sensor rows:', df['is_stuck_sensor'].sum())