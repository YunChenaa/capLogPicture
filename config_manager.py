"""
配置管理模块
保存和加载用户配置到本地JSON文件
"""
import json
import os


CONFIG_FILE = os.path.expanduser('~/.caplogpic_config.json')


def load_config():
    """
    加载配置文件

    Returns:
        dict: 配置字典，如果文件不存在返回空字典
    """
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f'[配置] 加载配置失败: {e}')
            return {}
    return {}


def save_config(config):
    """
    保存配置到文件

    Args:
        config: 配置字典
    """
    # 确保历史列表字段存在
    if 'download_dir_history' not in config:
        config['download_dir_history'] = []
    if 'output_history' not in config:
        config['output_history'] = []
    if 'host_program_history' not in config:
        config['host_program_history'] = []

    # 更新历史记录（最多保留5个，去重并保持顺序）
    if config.get('download_dir'):
        _update_history(config, 'download_dir', 'download_dir_history', max_items=5)
    if config.get('output'):
        _update_history(config, 'output', 'output_history', max_items=5)
    if config.get('host_program'):
        _update_history(config, 'host_program', 'host_program_history', max_items=5)

    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print(f'[配置] 已保存配置到: {CONFIG_FILE}')
    except Exception as e:
        print(f'[配置] 保存配置失败: {e}')


def _update_history(config, key, history_key, max_items=5):
    """更新历史记录列表

    Args:
        config: 配置字典
        key: 当前值的键名
        history_key: 历史列表的键名
        max_items: 最多保留的历史记录数量
    """
    current_value = config.get(key)
    if not current_value:
        return

    history = config.get(history_key, [])

    # 移除当前值（如果已存在）
    if current_value in history:
        history.remove(current_value)

    # 插入到最前面
    history.insert(0, current_value)

    # 保留最多max_items个
    config[history_key] = history[:max_items]


def get_config_value(key, default=None):
    """
    获取单个配置值

    Args:
        key: 配置键
        default: 默认值

    Returns:
        配置值或默认值
    """
    config = load_config()
    return config.get(key, default)


def set_config_value(key, value):
    """
    设置单个配置值

    Args:
        key: 配置键
        value: 配置值
    """
    config = load_config()
    config[key] = value
    save_config(config)
