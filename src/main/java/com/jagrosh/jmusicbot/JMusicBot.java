/*
 * Copyright 2016 John Grosh (jagrosh).
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.jagrosh.jmusicbot;

import java.security.Permission;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 *
 * @author John Grosh (jagrosh)
 * I'm just copying his code for learning purposes
 */

 public class JMusicBot {
    public final static Logger LOG = LoggerFactory.getLogger(JMusicBot.class);
    public final static Permission[] RECOMMENDED_PERMS = {Permission.MESSAGE_READ, Permission.MESSAGE_WRITE, Permission.MESSAGE_HISTORY, Permission.MESSAGE_ADD_REACTION,
                                Permission.MESSAGE_EMBED_LINKS, Permission.MESSAGE_ATTACH_FILES, Permission.MESSAGE_MANAGE, Permission.MESSAGE_EXT_EMOJI,
                                Permission.VOICE_CONNECT, Permission.VOICE_SPEAK, Permission.NICKNAME_CHANGE};
    public final static GatewayIntent[] INTENTS = {GatewayIntent.DIRECT_MESSAGES, GatewayIntent.GUILD_MESSAGES, GatewayIntent.GUILD_MESSAGE_REACTIONS, GatewayIntent.GUILD_VOICE_STATES};
    
    /**
     * @param args the command line arguments
     */

    public static void main(String[] args) {
        if (args.length > 0) 
            switch(args[0].toLowerCase()) {
                case "generate-config":
                    BotConfig.writeDefaultConfig();
                    return;
                // case if it doesnt go to any case
                default:
            }
        startBot();
    }

    public static void startBot()
    {
        Prompt prompt = new Prompt("JMusicBot");

        // startup checks
        OtherUtil.checkVersion(prompt);
        OtherUtil.checkJavaVersion(prompt);

        // load config
        BotConfig config = new BotConfig(prompt);
        config.load()
    }

}
 
